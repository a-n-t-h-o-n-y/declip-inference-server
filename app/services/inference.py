import hashlib
import importlib.metadata
import json
import logging
import shutil
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

LOGGER = logging.getLogger(__name__)

ARTIFACT_FILES = ("manifest.json", "resolved_config.json", "weights.pt", "checksums.sha256")


@dataclass(frozen=True)
class InferenceResult:
    model_output_gcs_uri: str
    output_duration_seconds: float
    output_size_bytes: int | None = None


class InferenceRunner(Protocol):
    def run(self, job: JobRecord, model: ModelSpec) -> InferenceResult:
        """Run inference for a job and return output metadata."""


@dataclass(frozen=True)
class DeclipArtifactPolicy:
    expected_artifact_format: str = "declip_state_dict_v1"
    expected_format_version: int = 1
    expected_model_family: str | None = None
    expected_model_name: str | None = None
    expected_model_version: str | None = None
    expected_declip_model_version: str | None = None
    expected_training_git_revision: str | None = None
    reject_dirty_training: bool = True


@dataclass(frozen=True)
class DeclipArtifactIdentity:
    artifact_uri: str
    artifact_format: str
    format_version: int
    model_family: str
    model_name: str
    model_version: str
    sample_rate_hz: int
    required_channels: int
    recommended_chunk_samples: int
    installed_declip_model_version: str


@dataclass(frozen=True)
class LoadedDeclipArtifact:
    model: torch.nn.Module
    identity: DeclipArtifactIdentity
    device: torch.device


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


class DeclipStateDictInferenceRunner:
    """Run a startup-loaded declip_state_dict_v1 model on canonical mono PCM audio."""

    def __init__(
        self,
        storage: StorageService,
        audio_probe: AudioProbeService,
        max_duration_seconds: int,
        loaded_artifact: LoadedDeclipArtifact,
    ) -> None:
        self._storage = storage
        self._audio_probe = audio_probe
        self._max_duration_seconds = max_duration_seconds
        self._loaded_artifact = loaded_artifact

    def run(self, job: JobRecord, model: ModelSpec) -> InferenceResult:
        if not job.model_input_gcs_uri or not job.model_output_gcs_uri:
            raise PermanentInferenceError(
                "invalid_job_audio", "Model audio storage is not configured."
            )
        identity = self._loaded_artifact.identity
        if model.sample_rate_hz != identity.sample_rate_hz:
            raise PermanentInferenceError(
                "unsupported_sample_rate", "Model input sample rate is not supported."
            )
        if (
            model.family != identity.model_family
            or model.model_name != identity.model_name
            or model.model_version != identity.model_version
        ):
            raise PermanentInferenceError(
                "model_identity_mismatch", "Requested model does not match loaded artifact."
            )

        with TemporaryDirectory(prefix=f"declip-{job.id}-") as temp_dir:
            local_input = Path(temp_dir) / "model-input.f32.wav"
            local_output = Path(temp_dir) / "model-output.f32.wav"
            self._storage.download(job.model_input_gcs_uri, local_input)
            metadata = self._audio_probe.probe(local_input)
            validate_canonical_pcm_metadata(
                metadata=metadata,
                expected_sample_rate_hz=identity.sample_rate_hz,
                expected_channels=identity.required_channels,
                max_duration_seconds=self._max_duration_seconds,
            )
            waveform = self._read_canonical_waveform(local_input, identity.required_channels)
            output = self._process_waveform(waveform)
            sf.write(
                str(local_output),
                output,
                identity.sample_rate_hz,
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
                    "model_family": identity.model_family,
                    "model_name": identity.model_name,
                    "model_version": identity.model_version,
                    "inference_backend": "declip_state_dict_v1",
                },
            )
            output_size = local_output.stat().st_size

        return InferenceResult(
            model_output_gcs_uri=job.model_output_gcs_uri,
            output_duration_seconds=metadata.duration_seconds,
            output_size_bytes=output_size,
        )

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

    def _process_waveform(self, waveform: np.ndarray) -> np.ndarray:
        identity = self._loaded_artifact.identity
        if waveform.shape[1] != identity.required_channels:
            raise PermanentInferenceError(
                "channel_count_mismatch", "Model input audio must be mono."
            )
        output = np.empty((waveform.shape[0], 1), dtype=np.float32)
        model = self._loaded_artifact.model
        device = self._loaded_artifact.device
        chunk_samples = identity.recommended_chunk_samples
        with torch.inference_mode():
            for start in range(0, waveform.shape[0], chunk_samples):
                stop = min(start + chunk_samples, waveform.shape[0])
                chunk = torch.from_numpy(
                    np.ascontiguousarray(waveform[start:stop, 0])
                ).to(device=device, dtype=torch.float32)
                original_samples = chunk.numel()
                if original_samples < chunk_samples:
                    padded = torch.zeros((1, chunk_samples), dtype=torch.float32, device=device)
                    padded[0, :original_samples] = chunk
                    batch = padded
                else:
                    batch = chunk.reshape(1, chunk_samples)
                reconstructed = model(batch)
                if reconstructed.shape != batch.shape:
                    raise RetryableDependencyError(
                        "invalid_model_output",
                        "Configured model returned an invalid audio shape.",
                    )
                if reconstructed.dtype != torch.float32:
                    raise RetryableDependencyError(
                        "invalid_model_output",
                        "Configured model returned an invalid audio dtype.",
                    )
                if not bool(torch.isfinite(reconstructed).all()):
                    raise RetryableDependencyError(
                        "invalid_model_output",
                        "Configured model returned non-finite audio.",
                    )
                output[start:stop, 0] = reconstructed[0, :original_samples].cpu().numpy()
        return output


def load_declip_artifact(
    *,
    artifact_uri: str,
    storage: StorageService,
    cache_dir: Path,
    device_name: str,
    policy: DeclipArtifactPolicy,
) -> LoadedDeclipArtifact:
    device = _resolve_device(device_name)
    local_dir = _prepare_artifact_dir(
        artifact_uri=artifact_uri,
        storage=storage,
        cache_dir=cache_dir,
    )
    _verify_checksums(local_dir)
    manifest = _read_json(local_dir / "manifest.json")
    _validate_manifest(manifest, policy)
    installed_version = _validate_provenance(manifest, policy)
    _validate_audio_contract(manifest)

    config_payload = _read_json(local_dir / "resolved_config.json")
    try:
        from declip.config import ExperimentConfig
        from declip.model import build_model
    except ImportError as error:
        raise ValueError("declip-model package is required to load declip artifacts") from error

    config = ExperimentConfig.model_validate(config_payload)
    model = build_model(config.model).to(device)

    payload = torch.load(local_dir / "weights.pt", map_location=device, weights_only=True)
    if payload.get("artifact_format") != manifest["artifact_format"]:
        raise ValueError("Weights artifact format does not match manifest")
    if payload.get("format_version") != manifest["format_version"]:
        raise ValueError("Weights format version does not match manifest")
    model.load_state_dict(payload["model_state_dict"])
    model.eval()

    identity = _artifact_identity(
        artifact_uri=artifact_uri,
        manifest=manifest,
        installed_declip_model_version=installed_version,
    )
    _smoke_test_model(model, identity, device)
    _log_artifact_identity(manifest, identity, device)
    return LoadedDeclipArtifact(model=model, identity=identity, device=device)


def _resolve_device(device_name: str) -> torch.device:
    if device_name == "cuda" and not torch.cuda.is_available():
        raise ValueError("DECLIP_DEVICE=cuda requires CUDA to be available")
    if device_name not in {"cpu", "cuda"}:
        raise ValueError("DECLIP_DEVICE must be cpu or cuda")
    return torch.device(device_name)


def _prepare_artifact_dir(
    *, artifact_uri: str, storage: StorageService, cache_dir: Path
) -> Path:
    artifact_hash = hashlib.sha256(artifact_uri.encode("utf-8")).hexdigest()
    local_dir = cache_dir / artifact_hash
    local_dir.mkdir(parents=True, exist_ok=True)
    if artifact_uri.startswith("gs://"):
        base_uri = artifact_uri.rstrip("/")
        for name in ARTIFACT_FILES:
            destination = local_dir / name
            storage.download(f"{base_uri}/{name}", destination)
    else:
        source_dir = Path(artifact_uri)
        if not source_dir.is_dir():
            raise ValueError(f"Artifact URI must be a GCS URI or local directory: {artifact_uri}")
        for name in ARTIFACT_FILES:
            source = source_dir / name
            destination = local_dir / name
            if not source.is_file():
                raise ValueError(f"Missing artifact file: {source}")
            shutil.copyfile(source, destination)
    return local_dir


def _verify_checksums(local_dir: Path) -> None:
    checksums_path = local_dir / "checksums.sha256"
    if not checksums_path.is_file():
        raise ValueError(f"Missing artifact file: {checksums_path}")
    expected_files = set(ARTIFACT_FILES) - {"checksums.sha256"}
    seen_files: set[str] = set()
    for line_number, raw_line in enumerate(checksums_path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            digest, relative_path = line.split("  ", 1)
        except ValueError as error:
            raise ValueError(f"Invalid checksum line {line_number}: {raw_line}") from error
        artifact_path = (local_dir / relative_path).resolve()
        if not artifact_path.is_relative_to(local_dir.resolve()):
            raise ValueError(
                f"Checksum references path outside artifact directory: {relative_path}"
            )
        if relative_path not in expected_files:
            raise ValueError(f"Checksum references unexpected artifact file: {relative_path}")
        if not artifact_path.is_file():
            raise ValueError(f"Missing artifact file: {artifact_path}")
        actual_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if actual_digest != digest:
            raise ValueError(f"Checksum mismatch for artifact file: {relative_path}")
        seen_files.add(relative_path)
    missing = expected_files - seen_files
    if missing:
        raise ValueError(f"Missing checksums for artifact files: {', '.join(sorted(missing))}")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ValueError(f"Missing artifact file: {path}") from error


def _validate_manifest(manifest: dict, policy: DeclipArtifactPolicy) -> None:
    if manifest.get("artifact_format") != policy.expected_artifact_format:
        raise ValueError("Unsupported artifact format")
    if manifest.get("format_version") != policy.expected_format_version:
        raise ValueError("Unsupported artifact format version")
    runtime_contract = manifest.get("runtime_contract") or {}
    if runtime_contract.get("runtime") != "pytorch_state_dict":
        raise ValueError("Unsupported artifact runtime")
    if runtime_contract.get("required_package") != "declip-model":
        raise ValueError("Unsupported artifact package")
    identity = manifest.get("serving_identity") or {}
    expected_identity = {
        "model_family": policy.expected_model_family,
        "model_name": policy.expected_model_name,
        "model_version": policy.expected_model_version,
    }
    for key, expected in expected_identity.items():
        if expected is not None and identity.get(key) != expected:
            raise ValueError(f"Artifact serving identity mismatch for {key}")


def _validate_provenance(manifest: dict, policy: DeclipArtifactPolicy) -> str:
    try:
        installed_version = importlib.metadata.version("declip-model")
    except importlib.metadata.PackageNotFoundError as error:
        raise ValueError("declip-model package is required to load declip artifacts") from error
    training_code = manifest["source"]["training_code"]
    training_version = str(training_code.get("declip_model_version", ""))
    expected_version = policy.expected_declip_model_version or installed_version
    if not _compatible_declip_model_version(training_version, expected_version):
        raise ValueError(
            "Artifact declip-model version does not match server policy"
        )
    if (
        policy.expected_training_git_revision
        and training_code.get("git_revision") != policy.expected_training_git_revision
    ):
        raise ValueError("Artifact training git revision does not match server policy")
    if policy.reject_dirty_training and bool(training_code.get("git_dirty")):
        raise ValueError("Dirty training artifacts are rejected by server policy")
    return installed_version


def _compatible_declip_model_version(actual: str, expected: str) -> bool:
    actual_parts = actual.split(".")
    expected_parts = expected.split(".")
    if len(actual_parts) >= 3 and len(expected_parts) >= 3:
        return actual_parts[:2] == expected_parts[:2]
    return actual == expected


def _validate_audio_contract(manifest: dict) -> None:
    audio_contract = manifest.get("audio_model_contract") or {}
    amplitude_policy = audio_contract.get("amplitude_policy") or {}
    tensor_contract = manifest.get("tensor_contract") or {}
    if audio_contract.get("required_channels") != 1:
        raise ValueError("declip_state_dict_v1 requires mono audio")
    if audio_contract.get("dtype") != "float32":
        raise ValueError("declip_state_dict_v1 requires float32 audio")
    if amplitude_policy.get("normalization_policy") != "none":
        raise ValueError("Unsupported audio normalization policy")
    if (
        amplitude_policy.get("amplitude_min") != -1.0
        or amplitude_policy.get("amplitude_max") != 1.0
    ):
        raise ValueError("Unsupported audio amplitude range")
    if tensor_contract.get("output_must_match_input_shape") is not True:
        raise ValueError("Artifact tensor contract must require same-shape output")
    if tensor_contract.get("output_must_be_finite") is not True:
        raise ValueError("Artifact tensor contract must require finite output")
    if int(manifest.get("recommended_chunk_samples", 0)) <= 0:
        raise ValueError("Artifact recommended chunk samples must be positive")


def _artifact_identity(
    *, artifact_uri: str, manifest: dict, installed_declip_model_version: str
) -> DeclipArtifactIdentity:
    serving_identity = manifest["serving_identity"]
    audio_contract = manifest["audio_model_contract"]
    return DeclipArtifactIdentity(
        artifact_uri=artifact_uri,
        artifact_format=manifest["artifact_format"],
        format_version=int(manifest["format_version"]),
        model_family=str(serving_identity["model_family"]),
        model_name=str(serving_identity["model_name"]),
        model_version=str(serving_identity["model_version"]),
        sample_rate_hz=int(audio_contract["sample_rate"]),
        required_channels=int(audio_contract["required_channels"]),
        recommended_chunk_samples=int(manifest["recommended_chunk_samples"]),
        installed_declip_model_version=installed_declip_model_version,
    )


def _smoke_test_model(
    model: torch.nn.Module, identity: DeclipArtifactIdentity, device: torch.device
) -> None:
    with torch.inference_mode():
        smoke_input = torch.zeros(
            (1, identity.recommended_chunk_samples),
            dtype=torch.float32,
            device=device,
        )
        smoke_output = model(smoke_input)
    if smoke_output.shape != smoke_input.shape:
        raise ValueError("Model smoke output shape does not match input")
    if smoke_output.dtype != torch.float32:
        raise ValueError("Model smoke output dtype is not float32")
    if not bool(torch.isfinite(smoke_output).all()):
        raise ValueError("Model smoke output is non-finite")


def _log_artifact_identity(
    manifest: dict, identity: DeclipArtifactIdentity, device: torch.device
) -> None:
    source = manifest.get("source") or {}
    checkpoint_metadata = source.get("checkpoint_metadata") or {}
    training_code = source.get("training_code") or {}
    LOGGER.info(
        "Loaded declip artifact",
        extra={
            "artifact_uri": identity.artifact_uri,
            "artifact_format": identity.artifact_format,
            "format_version": identity.format_version,
            "model_family": identity.model_family,
            "model_name": identity.model_name,
            "model_version": identity.model_version,
            "source_run_dir_name": source.get("run_dir_name"),
            "checkpoint": source.get("checkpoint"),
            "completed_epoch": checkpoint_metadata.get("completed_epoch"),
            "global_step": checkpoint_metadata.get("global_step"),
            "training_git_revision": training_code.get("git_revision"),
            "training_declip_model_version": training_code.get("declip_model_version"),
            "installed_declip_model_version": identity.installed_declip_model_version,
            "sample_rate_hz": identity.sample_rate_hz,
            "required_channels": identity.required_channels,
            "recommended_chunk_samples": identity.recommended_chunk_samples,
            "device": str(device),
        },
    )
