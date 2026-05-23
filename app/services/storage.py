from pathlib import Path
from typing import Protocol


class StorageService(Protocol):
    def download(self, gcs_uri: str, destination: Path) -> None:
        """Download an object to a local path."""

    def upload(self, source: Path, gcs_uri: str, content_type: str, metadata: dict[str, str]) -> None:
        """Upload a local path to an object URI."""
