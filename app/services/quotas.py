from math import ceil
from datetime import datetime, timezone
from typing import Protocol

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore

from app.core.errors import RetryableDependencyError
from app.models.domain import JobRecord


class QuotaService(Protocol):
    def release_reserved_seconds(self, job: JobRecord) -> int:
        """Release reserved seconds and return the released amount."""


def billable_seconds(job: JobRecord) -> int:
    return ceil(job.input_duration_seconds * job.input_channels)


class InMemoryQuotaService:
    def __init__(self) -> None:
        self.released_seconds_by_user: dict[str, int] = {}
        self._released_job_ids: set[str] = set()

    def release_reserved_seconds(self, job: JobRecord) -> int:
        if job.id in self._released_job_ids or job.reserved_quota_released_at is not None:
            return 0
        seconds = billable_seconds(job)
        self.released_seconds_by_user[job.user_id] = (
            self.released_seconds_by_user.get(job.user_id, 0) + seconds
        )
        self._released_job_ids.add(job.id)
        return seconds


class FirestoreQuotaService:
    def __init__(self, project_id: str | None = None, database: str = "(default)") -> None:
        self._client = firestore.Client(project=project_id, database=database)
        self._collection = self._client.collection("quotas")
        self._jobs_collection = self._client.collection("jobs")

    def release_reserved_seconds(self, job: JobRecord) -> int:
        seconds = billable_seconds(job)
        transaction = self._client.transaction()
        quota_ref = self._collection.document(job.user_id)
        job_ref = self._jobs_collection.document(job.id)

        @firestore.transactional
        def release_in_transaction(transaction: firestore.Transaction) -> int:
            snapshot = job_ref.get(transaction=transaction)
            if snapshot.exists and (snapshot.to_dict() or {}).get("reserved_quota_released_at"):
                return 0
            transaction.update(
                quota_ref,
                {"audio_seconds_reserved": firestore.Increment(-seconds)},
            )
            transaction.update(
                job_ref,
                {"reserved_quota_released_at": datetime.now(timezone.utc)},
            )
            return seconds

        try:
            return release_in_transaction(transaction)
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError(
                "database_unavailable", "Database is unavailable."
            ) from exc


_RETRYABLE_GOOGLE_EXCEPTIONS = (
    google_exceptions.Aborted,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
    google_exceptions.ResourceExhausted,
    google_exceptions.ServiceUnavailable,
    google_exceptions.TooManyRequests,
)
