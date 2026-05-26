from datetime import datetime, timezone

import pytest

from app.core.errors import RetryableDependencyError
from app.services.initial_dispatch import FirestoreInitialDispatchService
from app.services.queue import input_conversion_task_id


def test_firestore_claim_reads_shared_policy_transactionally_and_claims_waiting(
    monkeypatch,
) -> None:
    transaction = _RecordingTransaction()
    client = _Client(transaction)
    waiting = _job_data("job_waiting", "waiting")
    jobs = _JobsCollection([_Snapshot("job_waiting", waiting)])
    policy = _Document({"max_parallel_jobs_per_user": 1})
    service = FirestoreInitialDispatchService.__new__(FirestoreInitialDispatchService)
    service._client = client
    service._jobs = jobs
    service._policy = policy
    monkeypatch.setattr(
        "app.services.initial_dispatch.firestore.transactional", lambda function: function
    )

    claim = service.claim_next_input_dispatch("user_1")

    assert claim is not None
    assert claim.task_id == input_conversion_task_id("job_waiting")
    assert policy.read_transactions == [transaction]
    assert transaction.updates[0][1]["initial_dispatch_status"] == "pending"


def test_firestore_claim_resumes_pending_without_policy_read(monkeypatch) -> None:
    transaction = _RecordingTransaction()
    pending = _job_data("job_pending", "pending")
    pending["initial_dispatch_task_name"] = input_conversion_task_id("job_pending")
    service = FirestoreInitialDispatchService.__new__(FirestoreInitialDispatchService)
    service._client = _Client(transaction)
    service._jobs = _JobsCollection([_Snapshot("job_pending", pending)])
    service._policy = _Document(None)
    monkeypatch.setattr(
        "app.services.initial_dispatch.firestore.transactional", lambda function: function
    )

    claim = service.claim_next_input_dispatch("user_1")

    assert claim is not None
    assert claim.job.id == "job_pending"
    assert service._policy.read_transactions == []


def test_firestore_new_claim_fails_retryably_with_invalid_policy(monkeypatch) -> None:
    service = FirestoreInitialDispatchService.__new__(FirestoreInitialDispatchService)
    service._client = _Client(_RecordingTransaction())
    service._jobs = _JobsCollection(
        [_Snapshot("job_waiting", _job_data("job_waiting", "waiting"))]
    )
    service._policy = _Document({"max_parallel_jobs_per_user": 0})
    monkeypatch.setattr(
        "app.services.initial_dispatch.firestore.transactional", lambda function: function
    )

    with pytest.raises(RetryableDependencyError) as exc_info:
        service.claim_next_input_dispatch("user_1")

    assert exc_info.value.code == "dispatch_policy_unavailable"


def _job_data(job_id: str, initial_dispatch_status: str) -> dict:
    return {
        "id": job_id,
        "user_id": "user_1",
        "status": "queued",
        "model_family": "ddd-v1",
        "input_duration_seconds": 10,
        "input_sample_rate_hz": 44100,
        "input_channels": 2,
        "processing_stage": "awaiting_input_conversion",
        "initial_dispatch_status": initial_dispatch_status,
        "created_at": datetime(2026, 5, 21, tzinfo=timezone.utc),
    }


class _Client:
    def __init__(self, transaction: "_RecordingTransaction") -> None:
        self._transaction = transaction

    def transaction(self) -> "_RecordingTransaction":
        return self._transaction


class _JobsCollection:
    def __init__(self, snapshots: list["_Snapshot"]) -> None:
        self._snapshots = snapshots
        self.document_refs: dict[str, _Document] = {}

    def where(self, filter: object) -> "_JobsCollection":
        return self

    def stream(self, transaction: object | None = None) -> list["_Snapshot"]:
        return self._snapshots

    def document(self, job_id: str) -> "_Document":
        return self.document_refs.setdefault(job_id, _Document(None))


class _Document:
    def __init__(self, data: dict | None) -> None:
        self._data = data
        self.read_transactions: list[object] = []

    def get(self, transaction: object | None = None) -> "_Snapshot":
        self.read_transactions.append(transaction)
        return _Snapshot("document", self._data)


class _Snapshot:
    def __init__(self, snapshot_id: str, data: dict | None) -> None:
        self.id = snapshot_id
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict | None:
        return self._data


class _RecordingTransaction:
    def __init__(self) -> None:
        self.updates: list[tuple[object, dict]] = []

    def update(self, document: object, update: dict) -> None:
        self.updates.append((document, update))
