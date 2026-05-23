from math import ceil
from typing import Protocol

from google.cloud import firestore

from app.models.domain import JobRecord


class QuotaService(Protocol):
    def consume_reserved_seconds(self, job: JobRecord) -> int:
        """Move reserved seconds to used seconds and return the consumed amount."""

    def release_reserved_seconds(self, job: JobRecord) -> int:
        """Release reserved seconds and return the released amount."""


def billable_seconds(job: JobRecord) -> int:
    return ceil(job.input_duration_seconds * job.input_channels)


class InMemoryQuotaService:
    def __init__(self) -> None:
        self.used_seconds_by_user: dict[str, int] = {}
        self.released_seconds_by_user: dict[str, int] = {}

    def consume_reserved_seconds(self, job: JobRecord) -> int:
        seconds = billable_seconds(job)
        self.used_seconds_by_user[job.user_id] = self.used_seconds_by_user.get(job.user_id, 0) + seconds
        return seconds

    def release_reserved_seconds(self, job: JobRecord) -> int:
        seconds = billable_seconds(job)
        self.released_seconds_by_user[job.user_id] = (
            self.released_seconds_by_user.get(job.user_id, 0) + seconds
        )
        return seconds


class FirestoreQuotaService:
    def __init__(self, project_id: str | None = None, database: str = "(default)") -> None:
        self._client = firestore.Client(project=project_id, database=database)
        self._collection = self._client.collection("quotas")

    def consume_reserved_seconds(self, job: JobRecord) -> int:
        seconds = billable_seconds(job)
        self._collection.document(job.user_id).update(
            {
                "reserved_seconds": firestore.Increment(-seconds),
                "used_seconds": firestore.Increment(seconds),
            }
        )
        return seconds

    def release_reserved_seconds(self, job: JobRecord) -> int:
        seconds = billable_seconds(job)
        self._collection.document(job.user_id).update(
            {"reserved_seconds": firestore.Increment(-seconds)}
        )
        return seconds
