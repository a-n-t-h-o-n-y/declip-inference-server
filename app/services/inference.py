from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from app.core.errors import PermanentInferenceError
from app.models.domain import JobRecord, ModelSpec
from app.services.audio import AudioProbeService, validate_canonical_pcm_metadata
from app.services.storage import StorageService


@dataclass(frozen=True)
class InferenceResult:
    model_output_gcs_uri: str
    output_duration_seconds: float
    output_size_bytes: int | None = None


class InferenceRunner(Protocol):
    def run(self, job: JobRecord, model: ModelSpec) -> InferenceResult:
        """Run inference for a job and return output metadata."""


class PassthroughInferenceRunner:
    """Placeholder model runner that preserves the canonical PCM object bytes."""

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
        if not job.model_input_gcs_uri or not job.model_output_gcs_uri:
            raise PermanentInferenceError(
                "invalid_job_audio", "Model audio storage is not configured."
            )

        with TemporaryDirectory(prefix=f"declip-{job.id}-") as temp_dir:
            local_input = Path(temp_dir) / "model-input.f32.wav"
            self._storage.download(job.model_input_gcs_uri, local_input)
            metadata = self._audio_probe.probe(local_input)
            validate_canonical_pcm_metadata(
                metadata=metadata,
                expected_sample_rate_hz=model.sample_rate_hz,
                expected_channels=job.input_channels,
                max_duration_seconds=self._max_duration_seconds,
            )
            self._storage.upload(
                source=local_input,
                gcs_uri=job.model_output_gcs_uri,
                content_type="audio/wav",
                metadata={
                    "job_id": job.id,
                    "user_id": job.user_id,
                    "model_family": model.family,
                    "model_name": model.model_name,
                    "model_version": model.model_version,
                    "inference_backend": "passthrough",
                },
            )
            output_size = (
                local_input.stat().st_size if local_input.exists() else job.input_size_bytes
            )

        return InferenceResult(
            model_output_gcs_uri=job.model_output_gcs_uri,
            output_duration_seconds=metadata.duration_seconds,
            output_size_bytes=output_size,
        )
