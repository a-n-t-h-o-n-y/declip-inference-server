from dataclasses import dataclass
from typing import Protocol

from app.models.domain import JobRecord, ModelSpec


class PermanentInferenceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class InferenceResult:
    output_gcs_uri: str
    output_format: str
    output_content_type: str
    output_duration_seconds: float


class InferenceRunner(Protocol):
    def run(self, job: JobRecord, model: ModelSpec) -> InferenceResult:
        """Run inference for a job and return output metadata."""
