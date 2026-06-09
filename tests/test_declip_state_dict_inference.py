import hashlib
import json
import sys
import types
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import torch
from torch import nn

from app.core.errors import PermanentInferenceError, RetryableDependencyError
from app.models.domain import JobRecord, ModelSpec
from app.services.audio import AudioMetadata, StaticAudioProbeService
from app.services.inference import (
    DeclipArtifactPolicy,
    DeclipStateDictInferenceRunner,
    load_declip_artifact,
)
from app.services.storage import InMemoryStorageService


class ScaleModel(nn.Module):
    def __init__(self, scale: float = 1.0) -> None:
        super().__init__()
        self.scale = nn.Parameter(torch.tensor(scale, dtype=torch.float32))

    def forward(self, waveform: torch.Tensor) -> torch.Tensor:
        return waveform * self.scale


class WrongShapeModel(ScaleModel):
    def forward(self, waveform: torch.Tensor) -> torch.Tensor:
        return waveform[:, :-1]


class NonFiniteModel(ScaleModel):
    def forward(self, waveform: torch.Tensor) -> torch.Tensor:
        return waveform / torch.tensor(0.0, dtype=torch.float32, device=waveform.device)


def test_declip_artifact_loads_and_runtime_preserves_sample_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_declip(monkeypatch)
    artifact_dir = _write_artifact(tmp_path / "artifact", model=ScaleModel(2.0))
    loaded = load_declip_artifact(
        artifact_uri=str(artifact_dir),
        storage=InMemoryStorageService(),
        cache_dir=tmp_path / "cache",
        device_name="cpu",
        policy=_policy(),
    )
    storage = InMemoryStorageService(
        {"gs://bucket/model-input.f32.wav": _wav_bytes(np.linspace(-0.5, 0.5, 7, dtype=np.float32))}
    )
    runner = DeclipStateDictInferenceRunner(
        storage=storage,
        audio_probe=_probe(sample_rate=48000, channels=1),
        max_duration_seconds=1200,
        loaded_artifact=loaded,
    )

    result = runner.run(_job(), _model())

    output, sample_rate = sf.read(
        BytesIO(storage.objects["gs://bucket/model-output.f32.wav"]),
        dtype="float32",
        always_2d=True,
    )
    assert sample_rate == 48000
    assert output.shape == (7, 1)
    np.testing.assert_allclose(
        output[:, 0], np.linspace(-1.0, 1.0, 7, dtype=np.float32), atol=1e-6
    )
    assert result.output_size_bytes is not None
    assert storage.metadata["gs://bucket/model-output.f32.wav"]["inference_backend"] == (
        "declip_state_dict_v1"
    )


def test_checksum_mismatch_is_rejected_before_torch_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_declip(monkeypatch)
    artifact_dir = _write_artifact(tmp_path / "artifact")
    (artifact_dir / "weights.pt").write_bytes(b"not a torch file")

    with pytest.raises(ValueError, match="Checksum mismatch"):
        load_declip_artifact(
            artifact_uri=str(artifact_dir),
            storage=InMemoryStorageService(),
            cache_dir=tmp_path / "cache",
            device_name="cpu",
            policy=_policy(),
        )


def test_declip_model_patch_version_drift_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_declip(monkeypatch)
    artifact_dir = _write_artifact(
        tmp_path / "artifact",
        manifest=_deep_merge(
            _manifest(),
            {
                "source": {"training_code": {"declip_model_version": "0.1.0"}},
                "runtime_contract": {"declip_model_version": "0.1.0"},
            },
        ),
    )

    loaded = load_declip_artifact(
        artifact_uri=str(artifact_dir),
        storage=InMemoryStorageService(),
        cache_dir=tmp_path / "cache",
        device_name="cpu",
        policy=_policy(),
    )

    assert loaded.identity.installed_declip_model_version == "0.1.1"


def test_missing_artifact_file_is_rejected_with_path_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_declip(monkeypatch)
    artifact_dir = _write_artifact(tmp_path / "artifact")
    (artifact_dir / "manifest.json").unlink()

    with pytest.raises(ValueError, match="manifest.json"):
        load_declip_artifact(
            artifact_uri=str(artifact_dir),
            storage=InMemoryStorageService(),
            cache_dir=tmp_path / "cache",
            device_name="cpu",
            policy=_policy(),
        )


@pytest.mark.parametrize(
    ("manifest_update", "message"),
    [
        ({"artifact_format": "legacy"}, "Unsupported artifact format"),
        ({"format_version": 2}, "Unsupported artifact format version"),
        (
            {
                "serving_identity": {
                    "model_family": "other",
                    "model_name": "m",
                    "model_version": "v",
                }
            },
            "serving identity mismatch",
        ),
        (
            {"source": {"training_code": {"declip_model_version": "0.2.0"}}},
            "declip-model version",
        ),
        (
            {"source": {"training_code": {"git_dirty": True}}},
            "Dirty training artifacts",
        ),
    ],
)
def test_manifest_policy_failures_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_update: dict,
    message: str,
) -> None:
    _install_fake_declip(monkeypatch)
    artifact_dir = _write_artifact(
        tmp_path / "artifact",
        manifest=_deep_merge(_manifest(), manifest_update),
    )

    with pytest.raises(ValueError, match=message):
        load_declip_artifact(
            artifact_uri=str(artifact_dir),
            storage=InMemoryStorageService(),
            cache_dir=tmp_path / "cache",
            device_name="cpu",
            policy=_policy(),
        )


@pytest.mark.parametrize(
    ("model", "message"),
    [
        (WrongShapeModel(), "smoke output shape"),
        (NonFiniteModel(), "smoke output is non-finite"),
    ],
)
def test_bad_smoke_outputs_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: nn.Module,
    message: str,
) -> None:
    _install_fake_declip(monkeypatch, model=model)
    artifact_dir = _write_artifact(tmp_path / "artifact", model=model)

    with pytest.raises(ValueError, match=message):
        load_declip_artifact(
            artifact_uri=str(artifact_dir),
            storage=InMemoryStorageService(),
            cache_dir=tmp_path / "cache",
            device_name="cpu",
            policy=_policy(),
        )


def test_runtime_rejects_wrong_sample_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _loaded_artifact(tmp_path, monkeypatch)
    runner = DeclipStateDictInferenceRunner(
        storage=InMemoryStorageService(
            {"gs://bucket/model-input.f32.wav": _wav_bytes(np.zeros(4))}
        ),
        audio_probe=_probe(sample_rate=44100, channels=1),
        max_duration_seconds=1200,
        loaded_artifact=loaded,
    )

    with pytest.raises(PermanentInferenceError) as exc_info:
        runner.run(_job(), _model())

    assert exc_info.value.code == "sample_rate_mismatch"


def test_runtime_rejects_non_mono_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _loaded_artifact(tmp_path, monkeypatch)
    runner = DeclipStateDictInferenceRunner(
        storage=InMemoryStorageService(
            {"gs://bucket/model-input.f32.wav": _wav_bytes(np.zeros((4, 2)))}
        ),
        audio_probe=_probe(sample_rate=48000, channels=2),
        max_duration_seconds=1200,
        loaded_artifact=loaded,
    )

    with pytest.raises(PermanentInferenceError) as exc_info:
        runner.run(_job(), _model())

    assert exc_info.value.code == "channel_count_mismatch"


def test_runtime_rejects_non_finite_request_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_declip(monkeypatch)
    artifact_dir = _write_artifact(tmp_path / "artifact", model=ScaleModel())
    loaded = load_declip_artifact(
        artifact_uri=str(artifact_dir),
        storage=InMemoryStorageService(),
        cache_dir=tmp_path / "cache",
        device_name="cpu",
        policy=_policy(),
    )
    object.__setattr__(loaded, "model", NonFiniteModel())
    runner = DeclipStateDictInferenceRunner(
        storage=InMemoryStorageService(
            {"gs://bucket/model-input.f32.wav": _wav_bytes(np.zeros(4))}
        ),
        audio_probe=_probe(sample_rate=48000, channels=1),
        max_duration_seconds=1200,
        loaded_artifact=loaded,
    )

    with pytest.raises(RetryableDependencyError, match="non-finite"):
        runner.run(_job(), _model())


def _install_fake_declip(
    monkeypatch: pytest.MonkeyPatch, model: nn.Module | None = None
) -> None:
    declip_module = types.ModuleType("declip")
    config_module = types.ModuleType("declip.config")
    model_module = types.ModuleType("declip.model")

    class ExperimentConfig:
        def __init__(self, model_config: dict) -> None:
            self.model = model_config

        @classmethod
        def model_validate(cls, payload: dict) -> "ExperimentConfig":
            return cls(payload["model"])

    config_module.ExperimentConfig = ExperimentConfig
    model_module.build_model = lambda _config: model or ScaleModel()
    monkeypatch.setitem(sys.modules, "declip", declip_module)
    monkeypatch.setitem(sys.modules, "declip.config", config_module)
    monkeypatch.setitem(sys.modules, "declip.model", model_module)
    monkeypatch.setattr("importlib.metadata.version", lambda package: "0.1.1")


def _loaded_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_declip(monkeypatch)
    artifact_dir = _write_artifact(tmp_path / "artifact")
    return load_declip_artifact(
        artifact_uri=str(artifact_dir),
        storage=InMemoryStorageService(),
        cache_dir=tmp_path / "cache",
        device_name="cpu",
        policy=_policy(),
    )


def _write_artifact(
    artifact_dir: Path,
    *,
    model: nn.Module | None = None,
    manifest: dict | None = None,
) -> Path:
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest or _manifest()))
    (artifact_dir / "resolved_config.json").write_text(json.dumps({"model": {}}))
    torch.save(
        {
            "artifact_format": "declip_state_dict_v1",
            "format_version": 1,
            "model_state_dict": (model or ScaleModel()).state_dict(),
            "checkpoint_metadata": {
                "checkpoint": "best.pt",
                "completed_epoch": 1,
                "global_step": 123,
                "validation_loss": 0.1,
                "best_validation_loss": 0.1,
            },
        },
        artifact_dir / "weights.pt",
    )
    checksum_lines = []
    for name in ("manifest.json", "resolved_config.json", "weights.pt"):
        digest = hashlib.sha256((artifact_dir / name).read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  {name}")
    (artifact_dir / "checksums.sha256").write_text("\n".join(checksum_lines) + "\n")
    return artifact_dir


def _manifest() -> dict:
    return {
        "artifact_format": "declip_state_dict_v1",
        "format_version": 1,
        "serving_identity": {
            "model_family": "declip",
            "model_name": "tgram-deftan2-v005-48khz",
            "model_version": "20260605T224918Z",
        },
        "source": {
            "run_dir_name": "run",
            "checkpoint": "best.pt",
            "checkpoint_metadata": {
                "checkpoint": "best.pt",
                "completed_epoch": 1,
                "global_step": 123,
                "validation_loss": 0.1,
                "best_validation_loss": 0.1,
            },
            "config_fingerprint": "abc",
            "model_schema": "tgram_deftan2_v1",
            "training_code": {
                "git_revision": "abc123",
                "git_dirty": False,
                "declip_model_version": "0.1.1",
                "python_version": "3.13.12",
            },
        },
        "runtime_contract": {
            "runtime": "pytorch_state_dict",
            "required_package": "declip-model",
            "python_version": "3.13.12",
            "torch_version": "2.11.0",
            "declip_model_version": "0.1.1",
        },
        "audio_model_contract": {
            "sample_rate": 48000,
            "required_channels": 1,
            "dtype": "float32",
            "amplitude_policy": {
                "normalization_policy": "none",
                "amplitude_min": -1.0,
                "amplitude_max": 1.0,
            },
            "architecture": "tgramnet_deftan2_waveform",
            "model_config": {},
        },
        "tensor_contract": {
            "input_shape": "[batch, samples]",
            "output_shape": "[batch, samples]",
            "channels": "mono",
            "output_must_match_input_shape": True,
            "output_must_be_finite": True,
        },
        "recommended_chunk_samples": 4,
        "weight_dtype_policy": "preserve",
    }


def _policy() -> DeclipArtifactPolicy:
    return DeclipArtifactPolicy(
        expected_model_family="declip",
        expected_model_name="tgram-deftan2-v005-48khz",
        expected_model_version="20260605T224918Z",
        expected_declip_model_version="0.1.1",
        expected_training_git_revision="abc123",
    )


def _wav_bytes(waveform: np.ndarray) -> bytes:
    output = BytesIO()
    sf.write(output, waveform, 48000, format="WAV", subtype="FLOAT")
    return output.getvalue()


def _probe(sample_rate: int, channels: int) -> StaticAudioProbeService:
    return StaticAudioProbeService(
        AudioMetadata(
            duration_seconds=1.0,
            sample_rate_hz=sample_rate,
            channels=channels,
            format_name="wav",
            codec_name="pcm_f32le",
        )
    )


def _job() -> JobRecord:
    return JobRecord(
        id="job_1",
        user_id="user_1",
        status="processing",
        model_family="declip",
        input_gcs_uri=None,
        input_duration_seconds=1.0,
        input_sample_rate_hz=48000,
        input_channels=1,
        model_input_gcs_uri="gs://bucket/model-input.f32.wav",
        model_output_gcs_uri="gs://bucket/model-output.f32.wav",
        model_sample_rate_hz=48000,
    )


def _model() -> ModelSpec:
    return ModelSpec(
        family="declip",
        model_name="tgram-deftan2-v005-48khz",
        model_version="20260605T224918Z",
        sample_rate_hz=48000,
        artifact_uri="gs://models/declip/",
    )


def _deep_merge(base: dict, update: dict) -> dict:
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
