from dataclasses import dataclass
import secrets
from typing import Callable

from fastapi import HTTPException, status

from app.models.api import ProcessJobRequest
from app.models.domain import (
    AWAITING_INFERENCE_STAGE,
    AWAITING_OUTPUT_ENCODING_STAGE,
    INFERRING_STAGE,
    JobRecord,
)
from app.core.errors import PermanentInferenceError
from app.services.database import JobNotFoundError, JobRepository, JobStateConflictError
from app.services.inference import InferenceResult, InferenceRunner
from app.services.initial_dispatch import InitialDispatchService
from app.services.model_catalog import ModelCatalog
from app.services.quotas import QuotaService
from app.services.queue import ConversionQueue, FinalizationQueue


@dataclass(frozen=True)
class ProcessJobResult:
    job_id: str
    status: str
    processing_stage: str | None


class FakeInferenceRunner:
    def run(self, job: JobRecord, model: object) -> InferenceResult:
        output_uri = job.model_output_gcs_uri or (
            f"gs://local-fake-output/{job.user_id}/{job.id}/working/model-output.f32.wav"
        )
        return InferenceResult(
            model_output_gcs_uri=output_uri,
            output_duration_seconds=job.input_duration_seconds,
            output_size_bytes=job.input_size_bytes,
        )


class TaskProcessor:
    def __init__(
        self,
        jobs: JobRepository,
        quotas: QuotaService,
        catalog: ModelCatalog,
        inference: InferenceRunner,
        finalization_queue: FinalizationQueue,
        initial_dispatch: InitialDispatchService,
        conversion_queue: ConversionQueue,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._jobs = jobs
        self._quotas = quotas
        self._catalog = catalog
        self._inference = inference
        self._finalization_queue = finalization_queue
        self._initial_dispatch = initial_dispatch
        self._conversion_queue = conversion_queue
        self._request_id_factory = request_id_factory or (
            lambda: f"req_{secrets.token_urlsafe(18)}"
        )

    def process(self, payload: ProcessJobRequest) -> ProcessJobResult:
        try:
            job = self._jobs.get_job(payload.job_id)
        except JobNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "job_not_found", "message": "Job not found."},
            ) from None

        if job.user_id != payload.user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "job_not_found", "message": "Job not found."},
            )

        if job.status in {"succeeded", "failed", "cancelled"}:
            if job.status == "failed":
                self._quotas.release_reserved_seconds(job)
                self._dispatch_next_input(job.user_id, payload.trace_id)
            return ProcessJobResult(
                job_id=job.id, status=job.status, processing_stage=job.processing_stage
            )

        if job.status != "processing":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "job_state_conflict",
                    "message": "Job is not awaiting inference.",
                },
            )

        if job.processing_stage == AWAITING_OUTPUT_ENCODING_STAGE:
            self._finalization_queue.enqueue_finalize_output(payload)
            return ProcessJobResult(
                job_id=job.id, status=job.status, processing_stage=job.processing_stage
            )
        if job.processing_stage not in {AWAITING_INFERENCE_STAGE, INFERRING_STAGE}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "job_state_conflict", "message": "Job is not awaiting inference."},
            )

        if job.processing_stage == AWAITING_INFERENCE_STAGE:
            try:
                job = self._jobs.claim_inference(job.id)
            except JobStateConflictError:
                latest = self._jobs.get_job(payload.job_id)
                return ProcessJobResult(
                    job_id=latest.id,
                    status=latest.status,
                    processing_stage=latest.processing_stage,
                )

        try:
            if job.model_sample_rate_hz is None:
                raise PermanentInferenceError(
                    "missing_model_rate", "Model sample rate is not configured."
                )
            model = self._catalog.resolve_model(job.model_family, job.model_sample_rate_hz)
            output = self._inference.run(job, model)
        except LookupError:
            failed = self._jobs.mark_failed(
                job,
                code="unsupported_sample_rate",
                message="Unsupported audio sample rate for the requested model.",
            )
            self._quotas.release_reserved_seconds(failed)
            self._dispatch_next_input(failed.user_id, payload.trace_id)
            return ProcessJobResult(
                job_id=failed.id,
                status=failed.status,
                processing_stage=failed.processing_stage,
            )
        except PermanentInferenceError as exc:
            failed = self._jobs.mark_failed(job, code=exc.code, message=exc.message)
            self._quotas.release_reserved_seconds(failed)
            self._dispatch_next_input(failed.user_id, payload.trace_id)
            return ProcessJobResult(
                job_id=failed.id,
                status=failed.status,
                processing_stage=failed.processing_stage,
            )

        job.model_output_gcs_uri = output.model_output_gcs_uri
        job.processing_codec = "pcm_f32le"
        job.processing_sample_rate_hz = model.sample_rate_hz
        job.model_name = model.model_name
        job.model_version = model.model_version
        job.model_sample_rate_hz = model.sample_rate_hz
        try:
            awaiting_output = self._jobs.mark_awaiting_output_encoding(job)
        except JobStateConflictError:
            latest = self._jobs.get_job(payload.job_id)
            if latest.processing_stage != AWAITING_OUTPUT_ENCODING_STAGE:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "job_state_conflict", "message": "Job stage changed."},
                ) from None
            awaiting_output = latest
        self._finalization_queue.enqueue_finalize_output(payload)
        return ProcessJobResult(
            job_id=awaiting_output.id,
            status=awaiting_output.status,
            processing_stage=awaiting_output.processing_stage,
        )

    def _dispatch_next_input(self, user_id: str, trace_id: str | None) -> None:
        claim = self._initial_dispatch.claim_next_input_dispatch(user_id)
        if claim is None:
            return
        payload = ProcessJobRequest(
            job_id=claim.job.id,
            user_id=claim.job.user_id,
            attempt=1,
            request_id=self._request_id_factory(),
            trace_id=trace_id,
        )
        self._conversion_queue.enqueue_convert_input(payload, claim.task_id)
        self._initial_dispatch.mark_initial_dispatch_enqueued(claim.job.id)
