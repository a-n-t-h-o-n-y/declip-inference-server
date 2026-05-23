from copy import deepcopy
from typing import Protocol

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
