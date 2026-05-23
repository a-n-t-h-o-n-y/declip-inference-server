from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from app.models.domain import JobRecord, ModelSpec
from app.services.storage import StorageService


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
    output_size_bytes: int | None = None


class InferenceRunner(Protocol):
    def run(self, job: JobRecord, model: ModelSpec) -> InferenceResult:
        """Run inference for a job and return output metadata."""


class PassthroughInferenceRunner:
    """Initial deploy runner that copies input audio to output without DSP/model work."""

    def __init__(self, storage: StorageService) -> None:
        self._storage = storage

    def run(self, job: JobRecord, model: ModelSpec) -> InferenceResult:
        output_uri = job.output_gcs_uri or f"gs://local-fake-output/{job.user_id}/{job.id}/output.wav"
        content_type = job.input_content_type or "audio/wav"

        with TemporaryDirectory(prefix=f"declip-{job.id}-") as temp_dir:
            local_input = Path(temp_dir) / "input-audio"
            self._storage.download(job.input_gcs_uri, local_input)
            self._storage.upload(
                source=local_input,
                gcs_uri=output_uri,
                content_type=content_type,
                metadata={
                    "job_id": job.id,
                    "user_id": job.user_id,
                    "model_family": model.family,
                    "model_name": model.model_name,
                    "model_version": model.model_version,
                    "inference_backend": "passthrough",
                },
            )
            output_size = local_input.stat().st_size if local_input.exists() else job.input_size_bytes

        return InferenceResult(
            output_gcs_uri=output_uri,
            output_format=_format_from_content_type(content_type),
            output_content_type=content_type,
            output_duration_seconds=job.input_duration_seconds,
            output_size_bytes=output_size,
        )


def _format_from_content_type(content_type: str) -> str:
    if content_type == "audio/wav":
        return "wav"
    if "/" in content_type:
        return content_type.rsplit("/", maxsplit=1)[-1]
    return "wav"
