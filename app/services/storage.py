from pathlib import Path
from typing import Protocol

from google.api_core import exceptions as google_exceptions
from google.cloud import storage

from app.core.errors import RetryableDependencyError


class StorageService(Protocol):
    def download(self, gcs_uri: str, destination: Path) -> None:
        """Download an object to a local path."""

    def upload(self, source: Path, gcs_uri: str, content_type: str, metadata: dict[str, str]) -> None:
        """Upload a local path to an object URI."""


class InMemoryStorageService:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.metadata: dict[str, dict[str, str]] = {}
        self.content_types: dict[str, str] = {}

    def download(self, gcs_uri: str, destination: Path) -> None:
        destination.write_bytes(self.objects.get(gcs_uri, b""))

    def upload(self, source: Path, gcs_uri: str, content_type: str, metadata: dict[str, str]) -> None:
        self.objects[gcs_uri] = source.read_bytes()
        self.content_types[gcs_uri] = content_type
        self.metadata[gcs_uri] = dict(metadata)


class GcsStorageService:
    def __init__(self, project_id: str | None = None) -> None:
        self._client = storage.Client(project=project_id)

    def download(self, gcs_uri: str, destination: Path) -> None:
        bucket_name, object_name = _parse_gcs_uri(gcs_uri)
        try:
            self._client.bucket(bucket_name).blob(object_name).download_to_filename(destination)
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError("storage_unavailable", "Storage is unavailable.") from exc

    def upload(self, source: Path, gcs_uri: str, content_type: str, metadata: dict[str, str]) -> None:
        bucket_name, object_name = _parse_gcs_uri(gcs_uri)
        blob = self._client.bucket(bucket_name).blob(object_name)
        blob.metadata = metadata
        try:
            blob.upload_from_filename(source, content_type=content_type)
        except _RETRYABLE_GOOGLE_EXCEPTIONS as exc:
            raise RetryableDependencyError("storage_unavailable", "Storage is unavailable.") from exc


def _parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
    if not gcs_uri.startswith("gs://"):
        raise ValueError("GCS URI must start with gs://")
    path = gcs_uri.removeprefix("gs://")
    bucket_name, separator, object_name = path.partition("/")
    if not bucket_name or not separator or not object_name:
        raise ValueError("GCS URI must include bucket and object path")
    return bucket_name, object_name


_RETRYABLE_GOOGLE_EXCEPTIONS = (
    google_exceptions.Aborted,
    google_exceptions.DeadlineExceeded,
    google_exceptions.InternalServerError,
    google_exceptions.ResourceExhausted,
    google_exceptions.ServiceUnavailable,
    google_exceptions.TooManyRequests,
)
