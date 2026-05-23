from dataclasses import dataclass

from fastapi import HTTPException, status

from app.models.api import ProcessJobRequest
from app.models.domain import JobRecord, TERMINAL_JOB_STATUSES
from app.core.errors import PermanentInferenceError
from app.services.database import JobNotFoundError, JobRepository, JobStateConflictError
from app.services.inference import InferenceResult, InferenceRunner
from app.services.model_catalog import ModelCatalog
from app.services.quotas import QuotaService


@dataclass(frozen=True)
class ProcessJobResult:
    job_id: str
    status: str


class FakeInferenceRunner:
    def run(self, job: JobRecord, model: object) -> InferenceResult:
        output_uri = job.output_gcs_uri or f"gs://local-fake-output/{job.user_id}/{job.id}/output.wav"
        return InferenceResult(
            output_gcs_uri=output_uri,
            output_format="wav",
            output_content_type="audio/wav",
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
    ) -> None:
        self._jobs = jobs
        self._quotas = quotas
        self._catalog = catalog
        self._inference = inference

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

        if job.status in TERMINAL_JOB_STATUSES or job.status == "processing":
            return ProcessJobResult(job_id=job.id, status=job.status)

        if job.status != "queued":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "job_state_conflict",
                    "message": "Job is not ready for processing.",
                },
            )

        try:
            job = self._jobs.transition_queued_to_processing(job.id)
        except JobStateConflictError:
            latest = self._jobs.get_job(payload.job_id)
            return ProcessJobResult(job_id=latest.id, status=latest.status)

        try:
            model = self._catalog.resolve_model(job.model_family, job.input_sample_rate_hz)
            output = self._inference.run(job, model)
        except LookupError:
            failed = self._jobs.mark_failed(
                job,
                code="unsupported_sample_rate",
                message="Unsupported audio sample rate for the requested model.",
            )
            self._quotas.release_reserved_seconds(failed)
            return ProcessJobResult(job_id=failed.id, status=failed.status)
        except PermanentInferenceError as exc:
            failed = self._jobs.mark_failed(job, code=exc.code, message=exc.message)
            self._quotas.release_reserved_seconds(failed)
            return ProcessJobResult(job_id=failed.id, status=failed.status)

        job.output_gcs_uri = output.output_gcs_uri
        job.output_format = output.output_format
        job.output_content_type = output.output_content_type
        job.output_size_bytes = output.output_size_bytes
        job.output_duration_seconds = output.output_duration_seconds
        job.model_name = model.model_name
        job.model_version = model.model_version
        job.model_sample_rate_hz = model.sample_rate_hz
        succeeded = self._jobs.mark_succeeded(job)
        self._quotas.consume_reserved_seconds(succeeded)
        return ProcessJobResult(job_id=succeeded.id, status=succeeded.status)
