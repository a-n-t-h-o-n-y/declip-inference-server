from copy import deepcopy
from datetime import datetime, timezone
from typing import Protocol

from google.cloud import firestore

from app.models.domain import JobRecord


class JobNotFoundError(Exception):
    """Raised when a job record does not exist."""


class JobStateConflictError(Exception):
    """Raised when a queued-to-processing transition lost a race."""


class JobRepository(Protocol):
    def get_job(self, job_id: str) -> JobRecord:
        """Return a job record by ID."""

    def transition_queued_to_processing(self, job_id: str) -> JobRecord:
        """Atomically transition a queued job to processing."""

    def mark_succeeded(self, job: JobRecord) -> JobRecord:
        """Persist successful output metadata."""

    def mark_failed(self, job: JobRecord, code: str, message: str) -> JobRecord:
        """Persist permanent failure metadata."""


class InMemoryJobRepository:
    def __init__(self, jobs: list[JobRecord] | None = None) -> None:
        self._jobs = {job.id: deepcopy(job) for job in jobs or []}

    def add(self, job: JobRecord) -> None:
        self._jobs[job.id] = deepcopy(job)

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return deepcopy(self._jobs[job_id])
        except KeyError as exc:
            raise JobNotFoundError(job_id) from exc

    def transition_queued_to_processing(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job.status != "queued":
            raise JobStateConflictError(job_id)
        job.status = "processing"
        job.touch()
        self._jobs[job_id] = deepcopy(job)
        return job

    def mark_succeeded(self, job: JobRecord) -> JobRecord:
        job.status = "succeeded"
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
        snapshot = self._collection.document(job_id).get()
        if not snapshot.exists:
            raise JobNotFoundError(job_id)
        return _job_from_snapshot(job_id, snapshot.to_dict() or {})

    def transition_queued_to_processing(self, job_id: str) -> JobRecord:
        transaction = self._client.transaction()
        doc_ref = self._collection.document(job_id)

        @firestore.transactional
        def update_in_transaction(transaction: firestore.Transaction) -> JobRecord:
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise JobNotFoundError(job_id)
            job = _job_from_snapshot(job_id, snapshot.to_dict() or {})
            if job.status != "queued":
                raise JobStateConflictError(job_id)
            now = datetime.now(timezone.utc)
            transaction.update(
                doc_ref,
                {
                    "status": "processing",
                    "started_at": job.started_at or now,
                    "updated_at": now,
                },
            )
            job.status = "processing"
            job.started_at = job.started_at or now
            job.updated_at = now
            return job

        return update_in_transaction(transaction)

    def mark_succeeded(self, job: JobRecord) -> JobRecord:
        now = datetime.now(timezone.utc)
        self._collection.document(job.id).update(
            {
                "status": "succeeded",
                "model_name": job.model_name,
                "model_version": job.model_version,
                "model_sample_rate_hz": job.model_sample_rate_hz,
                "output_gcs_uri": job.output_gcs_uri,
                "output_format": job.output_format,
                "output_content_type": job.output_content_type,
                "output_size_bytes": job.output_size_bytes,
                "output_duration_seconds": job.output_duration_seconds,
                "error_code": None,
                "error_message": None,
                "completed_at": now,
                "updated_at": now,
            }
        )
        job.status = "succeeded"
        job.completed_at = now
        job.updated_at = now
        return deepcopy(job)

    def mark_failed(self, job: JobRecord, code: str, message: str) -> JobRecord:
        now = datetime.now(timezone.utc)
        self._collection.document(job.id).update(
            {
                "status": "failed",
                "error_code": code,
                "error_message": message,
                "completed_at": now,
                "updated_at": now,
            }
        )
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
        input_gcs_uri=str(data["input_gcs_uri"]),
        input_duration_seconds=float(data["input_duration_seconds"]),
        input_sample_rate_hz=int(data["input_sample_rate_hz"]),
        input_channels=int(data["input_channels"]),
        input_content_type=data.get("input_content_type"),
        input_size_bytes=data.get("input_size_bytes"),
        output_gcs_uri=data.get("output_gcs_uri"),
        model_name=data.get("model_name"),
        model_version=data.get("model_version"),
        model_sample_rate_hz=data.get("model_sample_rate_hz"),
        output_format=data.get("output_format"),
        output_content_type=data.get("output_content_type"),
        output_size_bytes=data.get("output_size_bytes"),
        output_duration_seconds=data.get("output_duration_seconds"),
        error_code=data.get("error_code"),
        error_message=data.get("error_message"),
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        updated_at=data.get("updated_at"),
    )
