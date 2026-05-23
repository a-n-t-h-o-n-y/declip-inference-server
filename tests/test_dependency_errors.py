import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as google_exceptions

from app.core.config import Settings
from app.core.errors import RetryableDependencyError
from app.main import create_app
from app.services.database import FirestoreJobRepository
from app.services.storage import GcsStorageService


TASK_ACCOUNT = "tasks@example.iam.gserviceaccount.com"


def test_retryable_dependency_errors_return_503() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.task_processor = _FailingProcessor()
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 1},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}", "x-request-id": "req_dep"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "storage_unavailable",
            "message": "Storage is unavailable.",
            "request_id": "req_dep",
        }
    }


def test_gcs_download_transient_failure_maps_to_retryable_dependency_error() -> None:
    service = GcsStorageService.__new__(GcsStorageService)
    service._client = _FailingStorageClient()

    with pytest.raises(RetryableDependencyError) as exc_info:
        service.download("gs://bucket/input.wav", destination=__file__)

    assert exc_info.value.code == "storage_unavailable"


def test_firestore_get_transient_failure_maps_to_retryable_dependency_error() -> None:
    repository = FirestoreJobRepository.__new__(FirestoreJobRepository)
    repository._collection = _FailingFirestoreCollection()

    with pytest.raises(RetryableDependencyError) as exc_info:
        repository.get_job("job_1")

    assert exc_info.value.code == "database_unavailable"


class _FailingProcessor:
    def process(self, payload: object) -> object:
        raise RetryableDependencyError("storage_unavailable", "Storage is unavailable.")


class _FailingStorageClient:
    def bucket(self, bucket_name: str) -> "_FailingBucket":
        return _FailingBucket()


class _FailingBucket:
    def blob(self, object_name: str) -> "_FailingBlob":
        return _FailingBlob()


class _FailingBlob:
    def download_to_filename(self, destination: object) -> None:
        raise google_exceptions.ServiceUnavailable("storage unavailable")


class _FailingFirestoreCollection:
    def document(self, document_id: str) -> "_FailingDocument":
        return _FailingDocument()


class _FailingDocument:
    def get(self) -> object:
        raise google_exceptions.DeadlineExceeded("database unavailable")
