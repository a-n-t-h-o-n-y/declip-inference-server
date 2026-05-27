from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import torch
from torch import nn

from app.core.errors import RetryableDependencyError
from app.models.domain import JobRecord, ModelSpec
from app.services.audio import AudioMetadata, StaticAudioProbeService
from app.services.inference import TorchscriptIdentityInferenceRunner
from app.services.storage import InMemoryStorageService


class WrongLengthModel(nn.Module):
    def forward(self, waveform: torch.Tensor) -> torch.Tensor:
        return waveform[:-1]


def _wav_bytes(waveform: np.ndarray) -> bytes:
    output = BytesIO()
    sf.write(output, waveform, 48000, format="WAV", subtype="FLOAT")
    return output.getvalue()


def _artifact_bytes(path: Path, model: nn.Module | None = None) -> bytes:
    example = torch.zeros(16, dtype=torch.float32)
    scripted = torch.jit.trace(model or nn.Identity(), example)
    scripted.save(str(path))
    return path.read_bytes()


def _job(channels: int) -> JobRecord:
    return JobRecord(
        id="job_1",
        user_id="user_1",
        status="processing",
        model_family="identity-stft-v0",
        input_gcs_uri=None,
        input_duration_seconds=1.0,
        input_sample_rate_hz=48000,
        input_channels=channels,
        model_input_gcs_uri="gs://bucket/model-input.f32.wav",
        model_output_gcs_uri="gs://bucket/model-output.f32.wav",
        model_sample_rate_hz=48000,
    )


def _model() -> ModelSpec:
    return ModelSpec(
        family="identity-stft-v0",
        model_name="identity-stft-v0-48khz",
        model_version="0.1.0",
        sample_rate_hz=48000,
        artifact_uri="gs://models/identity.pt",
    )


def _runner(storage: InMemoryStorageService, tmp_path: Path, channels: int):
    return TorchscriptIdentityInferenceRunner(
        storage=storage,
        audio_probe=StaticAudioProbeService(
            AudioMetadata(
                duration_seconds=1.0,
                sample_rate_hz=48000,
                channels=channels,
                format_name="wav",
                codec_name="pcm_f32le",
            )
        ),
        max_duration_seconds=1200,
        artifact_cache_dir=tmp_path / "cache",
        chunk_samples=8,
    )


def test_identity_runtime_preserves_stereo_audio_across_chunks(tmp_path: Path) -> None:
    waveform = np.column_stack(
        (
            np.linspace(-0.5, 0.5, 21, dtype=np.float32),
            np.linspace(0.25, -0.25, 21, dtype=np.float32),
        )
    )
    storage = InMemoryStorageService(
        {
            "gs://bucket/model-input.f32.wav": _wav_bytes(waveform),
            "gs://models/identity.pt": _artifact_bytes(tmp_path / "identity.pt"),
        }
    )

    result = _runner(storage, tmp_path, channels=2).run(_job(channels=2), _model())

    output, sample_rate = sf.read(
        BytesIO(storage.objects["gs://bucket/model-output.f32.wav"]),
        dtype="float32",
        always_2d=True,
    )
    assert sample_rate == 48000
    np.testing.assert_allclose(output, waveform, atol=0, rtol=0)
    assert result.output_size_bytes is not None
    assert storage.metadata["gs://bucket/model-output.f32.wav"]["inference_backend"] == (
        "identity_stft"
    )


def test_identity_runtime_rejects_invalid_artifact_output_shape(tmp_path: Path) -> None:
    waveform = np.zeros((16, 1), dtype=np.float32)
    storage = InMemoryStorageService(
        {
            "gs://bucket/model-input.f32.wav": _wav_bytes(waveform),
            "gs://models/identity.pt": _artifact_bytes(
                tmp_path / "invalid.pt", WrongLengthModel()
            ),
        }
    )

    with pytest.raises(RetryableDependencyError, match="invalid audio shape"):
        _runner(storage, tmp_path, channels=1).run(_job(channels=1), _model())
