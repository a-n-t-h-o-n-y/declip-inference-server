from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.core.errors import RetryableDependencyError
from app.models.domain import JobRecord
from app.services.database import InMemoryJobRepository
from app.services.queue import input_conversion_task_id


@dataclass(frozen=True)
class InitialDispatchClaim:
    job: JobRecord
    task_id: str


class InitialDispatchService(Protocol):
    def claim_next_input_dispatch(self, user_id: str) -> InitialDispatchClaim | None:
        """Resume a pending dispatch or transactionally claim one waiting job."""

    def mark_initial_dispatch_enqueued(self, job_id: str) -> None:
        """Record that creation of a claimed conversion task completed."""


class InMemoryInitialDispatchService:
    def __init__(
        self,
        jobs: InMemoryJobRepository,
        max_parallel_jobs_per_user: int | None = 1,
    ) -> None:
        self._jobs = jobs
        self._max_parallel_jobs_per_user = max_parallel_jobs_per_user

    def claim_next_input_dispatch(self, user_id: str) -> InitialDispatchClaim | None:
        user_jobs = self._jobs.list_user_jobs(user_id)
        pending = _oldest(
            job
            for job in user_jobs
            if job.status == "queued" and job.initial_dispatch_status == "pending"
        )
        if pending is not None:
            return InitialDispatchClaim(pending, _pending_task_id(pending))
        if not _valid_capacity(self._max_parallel_jobs_per_user):
            raise RetryableDependencyError(
                "dispatch_policy_unavailable", "Dispatch policy is missing or invalid."
            )
        if _occupied_capacity(user_jobs) >= self._max_parallel_jobs_per_user:
            return None
        waiting = _oldest(
            job
            for job in user_jobs
            if job.status == "queued" and job.initial_dispatch_status == "waiting"
        )
        if waiting is None:
            return None
        waiting.initial_dispatch_status = "pending"
        waiting.initial_dispatch_task_name = input_conversion_task_id(waiting.id)
        waiting.initial_dispatch_claimed_at = datetime.now(timezone.utc)
        waiting.touch()
        self._jobs.replace(waiting)
        return InitialDispatchClaim(waiting, waiting.initial_dispatch_task_name)

    def mark_initial_dispatch_enqueued(self, job_id: str) -> None:
        job = self._jobs.get_job(job_id)
        if job.initial_dispatch_status == "enqueued":
            return
        if job.status != "queued" or job.initial_dispatch_status != "pending":
            raise RetryableDependencyError(
                "dispatch_unavailable", "Pending input conversion dispatch is unavailable."
            )
        job.initial_dispatch_status = "enqueued"
        job.initial_dispatch_enqueued_at = datetime.now(timezone.utc)
        job.touch()
        self._jobs.replace(job)


class FirestoreInitialDispatchService:
    def __init__(self, project_id: str | None = None, database: str = "(default)") -> None:
        self._client = firestore.Client(project=project_id, database=database)
        self._jobs = self._client.collection("jobs")
        self._policy = self._client.collection("dispatch_policies").document("default")

    def claim_next_input_dispatch(self, user_id: str) -> InitialDispatchClaim | None:
        transaction = self._client.transaction()

        @firestore.transactional
        def claim_in_transaction(transaction: firestore.Transaction) -> InitialDispatchClaim | None:
            jobs = self._user_jobs(user_id, transaction)
            pending = _oldest(
                job
                for job in jobs
                if job.status == "queued" and job.initial_dispatch_status == "pending"
            )
            if pending is not None:
                return InitialDispatchClaim(pending, _pending_task_id(pending))
            policy_snapshot = self._policy.get(transaction=transaction)
            policy = policy_snapshot.to_dict() if policy_snapshot.exists else None
            maximum = policy.get("max_parallel_jobs_per_user") if policy else None
            if not _valid_capacity(maximum):
                raise RetryableDependencyError(
                    "dispatch_policy_unavailable", "Dispatch policy is missing or invalid."
                )
            if _occupied_capacity(jobs) >= maximum:
                return None
            waiting = _oldest(
                job
                for job in jobs
                if job.status == "queued" and job.initial_dispatch_status == "waiting"
            )
            if waiting is None:
                return None
            task_id = input_conversion_task_id(waiting.id)
            now = datetime.now(timezone.utc)
            transaction.update(
                self._jobs.document(waiting.id),
                {
                    "initial_dispatch_status": "pending",
                    "initial_dispatch_task_name": task_id,
                    "initial_dispatch_claimed_at": now,
                    "updated_at": now,
                },
            )
            waiting.initial_dispatch_status = "pending"
            waiting.initial_dispatch_task_name = task_id
            waiting.initial_dispatch_claimed_at = now
            waiting.updated_at = now
            return InitialDispatchClaim(waiting, task_id)

        try:
            return claim_in_transaction(transaction)
        except RetryableDependencyError:
            raise
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError(
                "database_unavailable", "Database is unavailable."
            ) from exc

    def mark_initial_dispatch_enqueued(self, job_id: str) -> None:
        transaction = self._client.transaction()
        doc_ref = self._jobs.document(job_id)

        @firestore.transactional
        def mark_in_transaction(transaction: firestore.Transaction) -> None:
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise RetryableDependencyError(
                    "dispatch_unavailable", "Pending input conversion dispatch is unavailable."
                )
            job = _job_from_dispatch_data(job_id, snapshot.to_dict() or {})
            if job.initial_dispatch_status == "enqueued":
                return
            if job.status != "queued" or job.initial_dispatch_status != "pending":
                raise RetryableDependencyError(
                    "dispatch_unavailable", "Pending input conversion dispatch is unavailable."
                )
            transaction.update(
                doc_ref,
                {
                    "initial_dispatch_status": "enqueued",
                    "initial_dispatch_enqueued_at": datetime.now(timezone.utc),
                },
            )

        try:
            mark_in_transaction(transaction)
        except RetryableDependencyError:
            raise
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError(
                "database_unavailable", "Database is unavailable."
            ) from exc

    def _user_jobs(self, user_id: str, transaction: firestore.Transaction) -> list[JobRecord]:
        query = self._jobs.where(filter=FieldFilter("user_id", "==", user_id))
        try:
            snapshots = query.stream(transaction=transaction)
            return [
                _job_from_dispatch_data(snapshot.id, snapshot.to_dict() or {})
                for snapshot in snapshots
            ]
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError(
                "database_unavailable", "Database is unavailable."
            ) from exc


def _job_from_dispatch_data(job_id: str, data: dict) -> JobRecord:
    return JobRecord(
        id=str(data.get("id") or job_id),
        user_id=str(data["user_id"]),
        status=str(data["status"]),
        model_family=str(data.get("model_family") or ""),
        input_gcs_uri=data.get("input_gcs_uri"),
        input_duration_seconds=float(data.get("input_duration_seconds") or 0),
        input_sample_rate_hz=int(data.get("input_sample_rate_hz") or 0),
        input_channels=int(data.get("input_channels") or 0),
        processing_stage=data.get("processing_stage"),
        initial_dispatch_status=data.get("initial_dispatch_status"),
        initial_dispatch_task_name=data.get("initial_dispatch_task_name"),
        initial_dispatch_claimed_at=data.get("initial_dispatch_claimed_at"),
        initial_dispatch_enqueued_at=data.get("initial_dispatch_enqueued_at"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def _oldest(jobs: Iterable[JobRecord]) -> JobRecord | None:
    candidates = list(jobs)
    if not candidates:
        return None
    if any(job.created_at is None for job in candidates):
        raise RetryableDependencyError(
            "dispatch_unavailable", "Queued job is missing dispatch ordering metadata."
        )
    return min(candidates, key=lambda job: job.created_at)


def _pending_task_id(job: JobRecord) -> str:
    expected = input_conversion_task_id(job.id)
    if job.initial_dispatch_task_name != expected:
        raise RetryableDependencyError(
            "dispatch_unavailable", "Pending input conversion dispatch identity is invalid."
        )
    return expected


def _occupied_capacity(jobs: list[JobRecord]) -> int:
    return sum(
        1
        for job in jobs
        if job.status == "processing"
        or (
            job.status == "queued"
            and job.initial_dispatch_status in {"pending", "enqueued"}
        )
    )


def _valid_capacity(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


_RETRYABLE_GOOGLE_EXCEPTIONS = (
    google_exceptions.Aborted,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
    google_exceptions.ResourceExhausted,
    google_exceptions.ServiceUnavailable,
    google_exceptions.TooManyRequests,
)
