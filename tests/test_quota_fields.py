from google.cloud import firestore

from app.models.domain import JobRecord
from app.services.quotas import FirestoreQuotaService


def test_firestore_quota_release_uses_public_api_field_names(monkeypatch) -> None:
    quota_document = _RecordingDocument()
    job_document = _RecordingDocument({"status": "failed"})
    service = FirestoreQuotaService.__new__(FirestoreQuotaService)
    service._collection = _RecordingCollection(quota_document)
    service._jobs_collection = _RecordingCollection(job_document)
    service._client = _RecordingClient()
    monkeypatch.setattr("app.services.quotas.firestore.transactional", lambda function: function)

    released = service.release_reserved_seconds(_job())

    assert released == 21
    assert set(quota_document.updates[0]) == {"audio_seconds_reserved"}
    assert isinstance(quota_document.updates[0]["audio_seconds_reserved"], firestore.Increment)
    assert set(job_document.updates[0]) == {"reserved_quota_released_at"}

def _job() -> JobRecord:
    return JobRecord(
        id="job_1",
        user_id="user_1",
        status="processing",
        model_family="ddd-v1",
        input_gcs_uri="gs://bucket/input.wav",
        input_duration_seconds=10.1,
        input_sample_rate_hz=44100,
        input_channels=2,
    )


class _RecordingCollection:
    def __init__(self, document: "_RecordingDocument") -> None:
        self._document = document

    def document(self, document_id: str) -> "_RecordingDocument":
        return self._document


class _RecordingDocument:
    def __init__(self, data: dict | None = None) -> None:
        self.updates: list[dict] = []
        self.data = data or {}

    def update(self, update: dict) -> None:
        self.updates.append(update)

    def get(self, transaction: object | None = None) -> "_Snapshot":
        return _Snapshot(self.data)


class _RecordingClient:
    def transaction(self) -> "_RecordingTransaction":
        return _RecordingTransaction()


class _RecordingTransaction:
    def update(self, document: _RecordingDocument, update: dict) -> None:
        document.update(update)


class _Snapshot:
    exists = True

    def __init__(self, data: dict) -> None:
        self._data = data

    def to_dict(self) -> dict:
        return self._data
