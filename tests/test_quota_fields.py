from google.cloud import firestore

from app.models.domain import JobRecord
from app.services.quotas import FirestoreQuotaService


def test_firestore_quota_consume_uses_public_api_field_names() -> None:
    service, document = _service_with_document()

    consumed = service.consume_reserved_seconds(_job())

    assert consumed == 21
    assert set(document.updates[0]) == {"audio_seconds_reserved", "audio_seconds_used"}
    assert isinstance(document.updates[0]["audio_seconds_reserved"], firestore.Increment)
    assert isinstance(document.updates[0]["audio_seconds_used"], firestore.Increment)


def test_firestore_quota_release_uses_public_api_field_names() -> None:
    service, document = _service_with_document()

    released = service.release_reserved_seconds(_job())

    assert released == 21
    assert set(document.updates[0]) == {"audio_seconds_reserved"}
    assert isinstance(document.updates[0]["audio_seconds_reserved"], firestore.Increment)


def _service_with_document() -> tuple[FirestoreQuotaService, "_RecordingDocument"]:
    document = _RecordingDocument()
    service = FirestoreQuotaService.__new__(FirestoreQuotaService)
    service._collection = _RecordingCollection(document)
    return service, document


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
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, update: dict) -> None:
        self.updates.append(update)
