from copy import deepcopy
from datetime import datetime, timezone
from typing import Protocol

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore

from app.core.errors import RetryableDependencyError
from app.models.domain import (
    AWAITING_INFERENCE_STAGE,
    AWAITING_OUTPUT_ENCODING_STAGE,
    INFERRING_STAGE,
    JobRecord,
)


class JobNotFoundError(Exception):
    """Raised when a job record does not exist."""


class JobStateConflictError(Exception):
    """Raised when a processing-stage transition lost a race."""


class JobRepository(Protocol):
    def get_job(self, job_id: str) -> JobRecord:
        """Return a job record by ID."""

    def claim_inference(self, job_id: str) -> JobRecord:
        """Atomically transition an inference-ready processing job to inferring."""

    def mark_awaiting_output_encoding(self, job: JobRecord) -> JobRecord:
        """Persist generated PCM metadata and transition to CPU finalization."""

    def mark_failed(self, job: JobRecord, code: str, message: str) -> JobRecord:
        """Persist permanent failure metadata."""


class InMemoryJobRepository:
    def __init__(self, jobs: list[JobRecord] | None = None) -> None:
        self._jobs = {job.id: deepcopy(job) for job in jobs or []}

    def add(self, job: JobRecord) -> None:
        self._jobs[job.id] = deepcopy(job)

    def replace(self, job: JobRecord) -> None:
        self._jobs[job.id] = deepcopy(job)

    def list_user_jobs(self, user_id: str) -> list[JobRecord]:
        return [deepcopy(job) for job in self._jobs.values() if job.user_id == user_id]

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return deepcopy(self._jobs[job_id])
        except KeyError as exc:
            raise JobNotFoundError(job_id) from exc

    def claim_inference(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job.status != "processing" or job.processing_stage != AWAITING_INFERENCE_STAGE:
            raise JobStateConflictError(job_id)
        job.processing_stage = INFERRING_STAGE
        job.touch()
        self._jobs[job_id] = deepcopy(job)
        return job

    def mark_awaiting_output_encoding(self, job: JobRecord) -> JobRecord:
        stored = self.get_job(job.id)
        if stored.status != "processing" or stored.processing_stage != INFERRING_STAGE:
            raise JobStateConflictError(job.id)
        job.processing_stage = AWAITING_OUTPUT_ENCODING_STAGE
        job.error_code = None
        job.error_message = None
        job.touch()
        self._jobs[job.id] = deepcopy(job)
        return deepcopy(job)

    def mark_failed(self, job: JobRecord, code: str, message: str) -> JobRecord:
        job.status = "failed"
        job.error_code = code
        job.error_message = message
        job.touch()
        self._jobs[job.id] = deepcopy(job)
        return deepcopy(job)


class FirestoreJobRepository:
    def __init__(self, project_id: str | None = None, database: str = "(default)") -> None:
        self._client = firestore.Client(project=project_id, database=database)
        self._collection = self._client.collection("jobs")

    def get_job(self, job_id: str) -> JobRecord:
        try:
            snapshot = self._collection.document(job_id).get()
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError("database_unavailable", "Database is unavailable.") from exc
        if not snapshot.exists:
            raise JobNotFoundError(job_id)
        return _job_from_snapshot(job_id, snapshot.to_dict() or {})

    def claim_inference(self, job_id: str) -> JobRecord:
        transaction = self._client.transaction()
        doc_ref = self._collection.document(job_id)

        @firestore.transactional
        def update_in_transaction(transaction: firestore.Transaction) -> JobRecord:
            try:
                snapshot = doc_ref.get(transaction=transaction)
            except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
                raise RetryableDependencyError("database_unavailable", "Database is unavailable.") from exc
            if not snapshot.exists:
                raise JobNotFoundError(job_id)
            job = _job_from_snapshot(job_id, snapshot.to_dict() or {})
            if job.status != "processing" or job.processing_stage != AWAITING_INFERENCE_STAGE:
                raise JobStateConflictError(job_id)
            now = datetime.now(timezone.utc)
            try:
                transaction.update(
                    doc_ref,
                    {
                        "processing_stage": INFERRING_STAGE,
                        "started_at": job.started_at or now,
                        "updated_at": now,
                    },
                )
            except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
                raise RetryableDependencyError("database_unavailable", "Database is unavailable.") from exc
            job.processing_stage = INFERRING_STAGE
            job.started_at = job.started_at or now
            job.updated_at = now
            return job

        try:
            return update_in_transaction(transaction)
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError("database_unavailable", "Database is unavailable.") from exc

    def mark_awaiting_output_encoding(self, job: JobRecord) -> JobRecord:
        transaction = self._client.transaction()
        doc_ref = self._collection.document(job.id)
        now = datetime.now(timezone.utc)

        @firestore.transactional
        def update_in_transaction(transaction: firestore.Transaction) -> None:
            try:
                snapshot = doc_ref.get(transaction=transaction)
            except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
                raise RetryableDependencyError("database_unavailable", "Database is unavailable.") from exc
            if not snapshot.exists:
                raise JobNotFoundError(job.id)
            current = _job_from_snapshot(job.id, snapshot.to_dict() or {})
            if current.status != "processing" or current.processing_stage != INFERRING_STAGE:
                raise JobStateConflictError(job.id)
            transaction.update(
                doc_ref,
                {
                    "processing_stage": AWAITING_OUTPUT_ENCODING_STAGE,
                    "model_name": job.model_name,
                    "model_version": job.model_version,
                    "model_sample_rate_hz": job.model_sample_rate_hz,
                    "model_output_gcs_uri": job.model_output_gcs_uri,
                    "processing_codec": job.processing_codec,
                    "processing_sample_rate_hz": job.processing_sample_rate_hz,
                    "error_code": None,
                    "error_message": None,
                    "updated_at": now,
                },
            )

        try:
            update_in_transaction(transaction)
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError("database_unavailable", "Database is unavailable.") from exc
        job.processing_stage = AWAITING_OUTPUT_ENCODING_STAGE
        job.updated_at = now
        return deepcopy(job)

    def mark_failed(self, job: JobRecord, code: str, message: str) -> JobRecord:
        now = datetime.now(timezone.utc)
        try:
            self._collection.document(job.id).update(
                {
                    "status": "failed",
                    "error_code": code,
                    "error_message": message,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError("database_unavailable", "Database is unavailable.") from exc
        job.status = "failed"
        job.error_code = code
        job.error_message = message
        job.completed_at = now
        job.updated_at = now
        return deepcopy(job)


def _job_from_snapshot(job_id: str, data: dict) -> JobRecord:
    return JobRecord(
        id=str(data.get("id") or job_id),
        user_id=str(data["user_id"]),
        status=str(data["status"]),
        model_family=str(data["model_family"]),
        input_gcs_uri=data.get("input_gcs_uri"),
        input_duration_seconds=float(data["input_duration_seconds"]),
        input_sample_rate_hz=int(data["input_sample_rate_hz"]),
        input_channels=int(data["input_channels"]),
        input_content_type=data.get("input_content_type"),
        input_size_bytes=data.get("input_size_bytes"),
        processing_stage=data.get("processing_stage"),
        model_input_gcs_uri=data.get("model_input_gcs_uri"),
        model_output_gcs_uri=data.get("model_output_gcs_uri"),
        output_gcs_uri=data.get("output_gcs_uri"),
        model_name=data.get("model_name"),
        model_version=data.get("model_version"),
        model_sample_rate_hz=(
            int(data["model_sample_rate_hz"]) if data.get("model_sample_rate_hz") is not None else None
        ),
        output_format=data.get("output_format"),
        output_content_type=data.get("output_content_type"),
        output_size_bytes=data.get("output_size_bytes"),
        output_duration_seconds=data.get("output_duration_seconds"),
        processing_codec=data.get("processing_codec"),
        processing_sample_rate_hz=data.get("processing_sample_rate_hz"),
        error_code=data.get("error_code"),
        error_message=data.get("error_message"),
        initial_dispatch_status=data.get("initial_dispatch_status"),
        initial_dispatch_task_name=data.get("initial_dispatch_task_name"),
        initial_dispatch_claimed_at=data.get("initial_dispatch_claimed_at"),
        initial_dispatch_enqueued_at=data.get("initial_dispatch_enqueued_at"),
        created_at=data.get("created_at"),
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        updated_at=data.get("updated_at"),
        reserved_quota_released_at=data.get("reserved_quota_released_at"),
    )


_RETRYABLE_GOOGLE_EXCEPTIONS = (
    google_exceptions.Aborted,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
    google_exceptions.ResourceExhausted,
    google_exceptions.ServiceUnavailable,
    google_exceptions.TooManyRequests,
)
