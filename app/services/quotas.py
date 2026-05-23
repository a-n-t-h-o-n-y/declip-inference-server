from math import ceil
from typing import Protocol

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
