import hashlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

import numpy as np
import soundfile as sf
import torch

from app.core.errors import PermanentInferenceError, RetryableDependencyError
from app.models.domain import JobRecord, ModelSpec
from app.services.audio import AudioProbeService, validate_canonical_pcm_metadata
from app.services.storage import StorageService

IDENTITY_CHUNK_SAMPLES_48KHZ = 480_000


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


class TorchscriptIdentityInferenceRunner:
    """Run the exported identity STFT artifact on canonical PCM audio."""

    def __init__(
        self,
        storage: StorageService,
        audio_probe: AudioProbeService,
        max_duration_seconds: int,
        artifact_cache_dir: Path,
        chunk_samples: int = IDENTITY_CHUNK_SAMPLES_48KHZ,
    ) -> None:
        self._storage = storage
        self._audio_probe = audio_probe
        self._max_duration_seconds = max_duration_seconds
        self._artifact_cache_dir = artifact_cache_dir
        self._chunk_samples = chunk_samples
        self._loaded_models: dict[str, torch.jit.ScriptModule] = {}

    def run(self, job: JobRecord, model: ModelSpec) -> InferenceResult:
        if not job.model_input_gcs_uri or not job.model_output_gcs_uri:
            raise PermanentInferenceError(
                "invalid_job_audio", "Model audio storage is not configured."
            )
        if model.sample_rate_hz != 48000:
            raise PermanentInferenceError(
                "unsupported_sample_rate", "Identity STFT supports only 48000 Hz."
            )

        with TemporaryDirectory(prefix=f"declip-{job.id}-") as temp_dir:
            local_input = Path(temp_dir) / "model-input.f32.wav"
            local_output = Path(temp_dir) / "model-output.f32.wav"
            self._storage.download(job.model_input_gcs_uri, local_input)
            metadata = self._audio_probe.probe(local_input)
            validate_canonical_pcm_metadata(
                metadata=metadata,
                expected_sample_rate_hz=model.sample_rate_hz,
                expected_channels=job.input_channels,
                max_duration_seconds=self._max_duration_seconds,
            )
            waveform = self._read_canonical_waveform(local_input, job.input_channels)
            scripted = self._load_model(model)
            output = self._process_channels(scripted, waveform)
            sf.write(
                str(local_output),
                output,
                model.sample_rate_hz,
                format="WAV",
                subtype="FLOAT",
            )
            self._storage.upload(
                source=local_output,
                gcs_uri=job.model_output_gcs_uri,
                content_type="audio/wav",
                metadata={
                    "job_id": job.id,
                    "user_id": job.user_id,
                    "model_family": model.family,
                    "model_name": model.model_name,
                    "model_version": model.model_version,
                    "inference_backend": "identity_stft",
                },
            )
            output_size = local_output.stat().st_size

        return InferenceResult(
            model_output_gcs_uri=job.model_output_gcs_uri,
            output_duration_seconds=metadata.duration_seconds,
            output_size_bytes=output_size,
        )

    def _load_model(self, model: ModelSpec) -> torch.jit.ScriptModule:
        cached = self._loaded_models.get(model.artifact_uri)
        if cached is not None:
            return cached
        artifact_name = (
            hashlib.sha256(model.artifact_uri.encode("utf-8")).hexdigest() + ".pt"
        )
        self._artifact_cache_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = self._artifact_cache_dir / artifact_name
        if not artifact_path.is_file():
            self._storage.download(model.artifact_uri, artifact_path)
        try:
            scripted = torch.jit.load(str(artifact_path), map_location="cpu").eval()
        except (OSError, RuntimeError, ValueError) as error:
            raise RetryableDependencyError(
                "model_unavailable", "Configured model artifact cannot be loaded."
            ) from error
        self._loaded_models[model.artifact_uri] = scripted
        return scripted

    @staticmethod
    def _read_canonical_waveform(path: Path, expected_channels: int) -> np.ndarray:
        try:
            waveform, _ = sf.read(str(path), dtype="float32", always_2d=True)
        except (OSError, RuntimeError) as error:
            raise PermanentInferenceError(
                "invalid_processing_audio", "Model input audio could not be decoded."
            ) from error
        if waveform.shape[1] != expected_channels:
            raise PermanentInferenceError(
                "channel_count_mismatch",
                "Model input audio channel count does not match the job.",
            )
        return waveform

    def _process_channels(
        self, model: torch.jit.ScriptModule, waveform: np.ndarray
    ) -> np.ndarray:
        output = np.empty_like(waveform)
        with torch.inference_mode():
            for channel_index in range(waveform.shape[1]):
                for start in range(0, waveform.shape[0], self._chunk_samples):
                    stop = min(start + self._chunk_samples, waveform.shape[0])
                    chunk = torch.from_numpy(
                        np.ascontiguousarray(waveform[start:stop, channel_index])
                    )
                    reconstructed = model(chunk)
                    if reconstructed.ndim != 1 or reconstructed.numel() != chunk.numel():
                        raise RetryableDependencyError(
                            "invalid_model_output",
                            "Configured model returned an invalid audio shape.",
                        )
                    if reconstructed.dtype != torch.float32:
                        raise RetryableDependencyError(
                            "invalid_model_output",
                            "Configured model returned an invalid audio dtype.",
                        )
                    output[start:stop, channel_index] = reconstructed.cpu().numpy()
        return output
