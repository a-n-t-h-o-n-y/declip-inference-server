from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class VerifiedServiceToken:
    email: str
    audience: str | None = None
    subject: str | None = None


TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
AWAITING_INFERENCE_STAGE = "awaiting_inference"
INFERRING_STAGE = "inferring"
AWAITING_OUTPUT_ENCODING_STAGE = "awaiting_output_encoding"


@dataclass
class JobRecord:
    id: str
    user_id: str
    status: str
    model_family: str
    input_gcs_uri: str | None
    input_duration_seconds: float
    input_sample_rate_hz: int
    input_channels: int
    input_content_type: str | None = None
    input_size_bytes: int | None = None
    processing_stage: str | None = None
    model_input_gcs_uri: str | None = None
    model_output_gcs_uri: str | None = None
    output_gcs_uri: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    model_sample_rate_hz: int | None = None
    output_format: str | None = None
    output_content_type: str | None = None
    output_size_bytes: int | None = None
    output_duration_seconds: float | None = None
    processing_codec: str | None = None
    processing_sample_rate_hz: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    initial_dispatch_status: str | None = None
    initial_dispatch_task_name: str | None = None
    initial_dispatch_claimed_at: datetime | None = None
    initial_dispatch_enqueued_at: datetime | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None
    reserved_quota_released_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


@dataclass(frozen=True)
class ModelSpec:
    family: str
    model_name: str
    model_version: str
    sample_rate_hz: int
    artifact_uri: str
    runtime: str = "pytorch"


@dataclass(frozen=True)
class InferenceInput:
    job_id: str
    sample_rate_hz: int
    channel_index: int
    input_wav_path: Path
    output_wav_path: Path


@dataclass(frozen=True)
class InferenceOutput:
    output_wav_path: Path
    processed_samples: int
    model_name: str
    model_version: str


class DeclippingModel(Protocol):
    spec: ModelSpec

    def load(self, device: object) -> None:
        """Load weights and prepare the model for inference."""

    def process_channel(self, request: InferenceInput) -> InferenceOutput:
        """Run declipping for one mono WAV channel."""
