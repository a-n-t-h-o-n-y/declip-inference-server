from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from app.core.errors import PermanentInferenceError
from app.models.domain import JobRecord, ModelSpec
from app.services.audio import AudioProbeService, validate_audio_metadata
from app.services.storage import StorageService


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

    def __init__(
        self,
        storage: StorageService,
        audio_probe: AudioProbeService,
        max_duration_seconds: int,
    ) -> None:
        self._storage = storage
        self._audio_probe = audio_probe
        self._max_duration_seconds = max_duration_seconds

    def run(self, job: JobRecord, model: ModelSpec) -> InferenceResult:
        output_uri = job.output_gcs_uri or output_uri_from_input(job.input_gcs_uri, job.id)
        content_type = job.input_content_type or "audio/wav"

        with TemporaryDirectory(prefix=f"declip-{job.id}-") as temp_dir:
            local_input = Path(temp_dir) / "input-audio"
            self._storage.download(job.input_gcs_uri, local_input)
            metadata = self._audio_probe.probe(local_input)
            validate_audio_metadata(
                metadata=metadata,
                expected_sample_rate_hz=model.sample_rate_hz,
                expected_channels=job.input_channels,
                max_duration_seconds=self._max_duration_seconds,
            )
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
            output_duration_seconds=metadata.duration_seconds,
            output_size_bytes=output_size,
        )


def _format_from_content_type(content_type: str) -> str:
    if content_type == "audio/wav":
        return "wav"
    if "/" in content_type:
        return content_type.rsplit("/", maxsplit=1)[-1]
    return "wav"


def output_uri_from_input(input_gcs_uri: str, job_id: str) -> str:
    input_marker = f"/jobs/{job_id}/input/"
    if input_marker in input_gcs_uri:
        prefix, _, filename = input_gcs_uri.partition(input_marker)
        safe_filename = filename.rsplit("/", maxsplit=1)[-1] or f"{job_id}.wav"
        return f"{prefix}/jobs/{job_id}/output/{safe_filename}"

    if not input_gcs_uri.startswith("gs://"):
        raise PermanentInferenceError("invalid_storage_uri", "Input storage URI is invalid.")
    bucket_and_path = input_gcs_uri.removeprefix("gs://")
    bucket, separator, _object_name = bucket_and_path.partition("/")
    if not bucket or not separator:
        raise PermanentInferenceError("invalid_storage_uri", "Input storage URI is invalid.")
    return f"gs://{bucket}/users/unknown/jobs/{job_id}/output/{job_id}.wav"
