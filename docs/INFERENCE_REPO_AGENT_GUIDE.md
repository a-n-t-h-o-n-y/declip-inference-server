# Inference Repository Agent Guide

This document is intended to be copied into a new, initially empty repository
for the Declip private inference service. It gives an AI coding agent enough
context to implement the service consistently with the existing public API
backend while keeping the inference runtime independently deployable.

The existing public API repository is `declip-backend-server`. The new
repository should own GPU inference, model loading, model catalog source of
truth, and private service endpoints. Both services will run in the same Google
Cloud project and use the same Firestore database, GCS audio bucket, Cloud
Tasks queue, and service-account based IAM model.

## Purpose

Build a production-ready FastAPI service for private audio declipping inference.
The service is invoked by Cloud Tasks and by the public API backend over
service-to-service authenticated HTTP. It should not be directly callable by the
frontend.

The service owns:

- Private model catalog source of truth.
- Private model catalog discovery for the public API.
- Audio download, probing, conversion, channel splitting, inference, channel
  recombination, and output upload.
- Concrete model resolution by family and sample rate.
- Firestore job status updates during task processing.
- Idempotent processing behavior for Cloud Tasks retries.
- GPU-oriented deployment configuration and runtime dependencies.

The service should not own:

- Auth0 user authentication for browsers.
- Public frontend API routes.
- Signed upload URL creation for user uploads.
- User quota policy decisions beyond consuming or releasing already reserved
  quota according to the shared job lifecycle.
- Frontend response schemas.

## Target Architecture

```txt
Frontend
    |
    | HTTPS + Auth0 access token
    v
Public API Cloud Run service
    |
    | private service-to-service GET /internal/model-catalog
    v
Private inference Cloud Run service

Public API Cloud Run service
    |
    | creates HTTP Cloud Task with OIDC token
    v
Cloud Tasks queue
    |
    | POST /tasks/process-job with OIDC token
    v
Private inference Cloud Run service
    |
    | Firestore jobs/quotas + GCS audio bucket + model artifacts
    v
GCP resources shared with the public API service
```

The public API remains the only browser-facing API. The inference service must
be private. Cloud Tasks and the public API service call it with Google-issued
OIDC identity tokens.

## Initial Repository Shape

Prefer a small, explicit FastAPI layout:

```txt
app/
  main.py
  api/
    dependencies.py
    routes/
      health.py
      internal.py
      tasks.py
  core/
    config.py
    errors.py
    logging.py
  models/
    api.py
    domain.py
  services/
    audio.py
    database.py
    inference.py
    model_catalog.py
    model_runtime.py
    storage.py
    task_auth.py
    task_processing.py
    quotas.py
config/
  models.yaml
  app.yaml
docs/
  ARCHITECTURE.md
  API_CONTRACT.md
  INFERENCE.md
  OPERATIONS.md
  TESTING.md
tests/
Dockerfile
pyproject.toml
```

Keep route handlers thin. Put business logic behind service interfaces so tests
can use fakes without Google Cloud credentials, GPU access, or model artifacts.

## Required Endpoints

### `GET /health`

Public unauthenticated liveness endpoint. It should not expose dependency
details, credentials, bucket names, model artifact URIs, or project internals.

Example response:

```json
{
  "status": "ok",
  "app_name": "declip-inference-server",
  "environment": "dev",
  "version": "0.1.0"
}
```

### `GET /version`

Optional unauthenticated build metadata endpoint. It should expose only safe
build fields.

Example response:

```json
{
  "app_name": "declip-inference-server",
  "environment": "dev",
  "version": "0.1.0"
}
```

### `GET /internal/model-catalog`

Private endpoint called by the public API backend. This is the preferred source
of truth for frontend-safe model discovery. The public API should call this
endpoint with service-to-service authentication, cache the response, expose a
sanitized public `GET /models` endpoint to the frontend, and use the catalog for
job validation.

Auth: required Google OIDC service token from an allowed caller service account.
For dev, the allowed caller should be the public API Cloud Run service account.

This endpoint must not return private artifact URIs, local filesystem paths,
internal bucket paths, GPU sizing, or implementation-only constraints unless the
public API genuinely needs them for validation.

Recommended response:

```json
{
  "catalog_version": "0.1.0",
  "model_families": [
    {
      "family": "ddd-v1",
      "display_name": "DDD v1",
      "description": "General declipping model family.",
      "enabled": true,
      "supported_sample_rates_hz": [44100, 48000],
      "default_output_format": "wav"
    }
  ]
}
```

Rules:

- Return only enabled model families by default.
- Preserve stable `family` identifiers because clients submit them in
  `POST /jobs` to the public API.
- `supported_sample_rates_hz` must be derived from enabled concrete models in
  the private model catalog.
- Disabled or experimental families should not appear unless a deliberate
  rollout mechanism is added.
- `catalog_version` can be a semantic version, date, commit SHA, or configured
  string. It is useful for logs and cache invalidation.

### `POST /tasks/process-job`

Private endpoint called only by Cloud Tasks. It processes a queued job.

Auth: required Google OIDC task token from the configured Cloud Tasks service
account.

Request:

```json
{
  "job_id": "job_opaque_id",
  "user_id": "auth0_subject",
  "attempt": 1,
  "request_id": "req_...",
  "trace_id": "trace-optional"
}
```

Response `200`:

```json
{
  "job_id": "job_opaque_id",
  "status": "succeeded",
  "attempt": 1
}
```

Behavior:

- Validate task OIDC token before reading or mutating job data.
- Fetch the job from Firestore by `job_id`.
- Validate that `job.user_id` matches `user_id` in the task payload.
- If status is already `processing`, `succeeded`, or `failed`, return the
  current status without duplicating output or quota mutations.
- If status is not `queued`, return a conflict response or the current terminal
  status according to the idempotency rules.
- Transition `queued` to `processing`.
- Download input audio from the job's `input_gcs_uri`.
- Probe and validate audio metadata defensively.
- Resolve the concrete model by `job.model_family` and sample rate.
- Convert audio to model-ready WAV when needed.
- Split channels, process each mono channel, and recombine.
- Upload final output to GCS.
- Store output metadata on the job.
- Transition the job to `succeeded`.
- Move reserved quota seconds to used quota seconds.
- On permanent inference failure, set user-safe failure fields, transition the
  job to `failed`, and release reserved quota seconds.
- Let clearly transient infrastructure failures surface as non-2xx responses
  only when a Cloud Tasks retry is desirable.

## Error Shape

Use the same error shape as the public API service:

```json
{
  "error": {
    "code": "invalid_task_token",
    "message": "Unauthorized.",
    "request_id": "req_..."
  }
}
```

Use predictable HTTP status codes:

- `400` for malformed or invalid requests.
- `401` for missing or invalid service tokens.
- `403` for authenticated service accounts that are not allowed.
- `404` for missing jobs or ownership mismatch.
- `409` for non-processable job states.
- `422` for request schema validation errors.
- `500` for unexpected failures.
- `503` for dependency failures that should be retried.

Do not leak model paths, stack traces, signed URLs, bearer tokens, raw user data,
or private GCS object names in user-safe error messages.

## Model Catalog Ownership

The inference repository should own the private model config file.

Example private `config/models.yaml`:

```yaml
catalog_version: 0.1.0
model_families:
  - family: ddd-v1
    enabled: true
    display_name: DDD v1
    description: General declipping model family.
    default_output_format: wav
    models:
      - sample_rate_hz: 44100
        model_name: ddd-v1-44k
        model_version: 1.0.0
        artifact_uri: gs://declip-models-dev/ddd-v1/44100/model.pt
        runtime: pytorch
        enabled: true
      - sample_rate_hz: 48000
        model_name: ddd-v1-48k
        model_version: 1.0.0
        artifact_uri: gs://declip-models-dev/ddd-v1/48000/model.pt
        runtime: pytorch
        enabled: true
```

Validation requirements:

- Config files are non-secret and can be committed.
- Invalid config must fail startup.
- Family identifiers must be unique.
- Enabled families must include at least one enabled model.
- Sample rates must be unique within a family.
- `artifact_uri` must be present for private runtime use but omitted from
  `GET /internal/model-catalog`.
- Runtime should initially be `pytorch`.
- Launch artifact format should initially be TorchScript `.pt` unless the model
  implementation requires a different explicit decision.

The public API should not need private artifact URIs. If it needs validation
fields, expose them deliberately through `GET /internal/model-catalog` rather
than sharing the full private config.

## Model Runtime Interface

Define a stable Python boundary for model implementations. Keep model code
independent from FastAPI, Firestore, Auth0, and public response schemas.

Suggested interface:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import torch


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

    def load(self, device: torch.device) -> None:
        """Load weights and prepare the model for inference."""

    def process_channel(self, request: InferenceInput) -> InferenceOutput:
        """Run declipping for one mono WAV channel."""
```

Runtime rules:

- The inference service owns artifact download or local cache management.
- The inference service owns audio conversion, channel splitting, and channel
  recombination.
- The model implementation receives mono WAV files at the configured sample
  rate.
- The model implementation should not read Firestore or GCS directly.
- The model implementation should not update job status or quota.

## Audio Processing Rules

Launch-supported upload formats are:

- WAV
- MP3
- AIFF
- FLAC
- M4A
- AAC
- OGG

Rules:

- Validate decoded audio, not just filename or content type.
- Maximum decoded duration is currently 20 minutes per job.
- File-size limit is enforced by the public API before upload URL creation, but
  the inference service should still defend against unexpected inputs.
- Bit depth does not select the model.
- Sample rate selects the concrete model within a family.
- Unsupported sample rates should fail with a clear permanent error.
- Do not silently resample unsupported sample rates for launch.
- Multi-channel audio is processed per channel and recombined with the original
  channel layout when supported.
- WAV output is the default launch output, including for compressed inputs.
- Temporary files should be written under safe temporary directories and cleaned
  up after processing.

Use `ffprobe` or equivalent for metadata probing and `ffmpeg` or equivalent for
conversion. Wrap subprocess execution in a service boundary so tests can fake
metadata and conversion behavior.

## Shared Firestore Data Model

Use the same Firestore collections as the public API:

```txt
users/{uid}
jobs/{job_id}
quotas/{uid}
```

The inference service primarily reads and updates:

- `jobs/{job_id}`
- `quotas/{uid}`

Important job fields:

- `id`
- `user_id`
- `status`
- `model_family`
- `model_name`
- `model_version`
- `model_sample_rate_hz`
- `input_gcs_uri`
- `working_wav_gcs_uri`
- `output_gcs_uri`
- `input_filename`
- `input_content_type`
- `input_format`
- `input_size_bytes`
- `input_duration_seconds`
- `input_sample_rate_hz`
- `input_channels`
- `input_bit_depth`
- `processing_format`
- `output_format`
- `output_content_type`
- `output_size_bytes`
- `output_duration_seconds`
- `error_code`
- `error_message`
- `created_at`
- `updated_at`
- `queued_at`
- `started_at`
- `completed_at`

Job statuses:

- `created`
- `uploading`
- `uploaded`
- `queued`
- `processing`
- `succeeded`
- `failed`
- `cancelled`

The inference service should only process `queued` jobs. `succeeded`, `failed`,
and `cancelled` are terminal.

Quota accounting:

- The public API reserves channel-weighted decoded audio seconds before
  enqueueing.
- On success, inference moves reserved seconds to used seconds.
- On permanent failure, inference releases reserved seconds.
- The billable seconds calculation should match the public API:
  `ceil(input_duration_seconds * input_channels)` when channel-weighted quota is
  enabled.

Use transactions or preconditions for status and quota mutations where
concurrent retries could otherwise double-consume quota or overwrite terminal
status.

## Shared GCS Layout

The shared audio bucket stores input, working, and output audio. Object paths
are created by the public API and stored as GCS URIs on job records.

Suggested layout:

```txt
users/{uid}/jobs/{job_id}/input/{safe_filename}
users/{uid}/jobs/{job_id}/working/{job_id}.wav
users/{uid}/jobs/{job_id}/output/{safe_filename}
```

Rules:

- Buckets must not be public.
- Do not create signed URLs from the inference service unless a future internal
  use case requires it.
- Store output object metadata where useful: job ID, user ID, content type,
  model family, model name, model version.
- Do not log signed URLs or raw object contents.

## Google Cloud Setup

The new service uses the same project and core resources already defined for the
public API environment.

Required APIs:

- Artifact Registry
- Cloud Build
- Cloud Run
- Cloud Tasks
- Firestore
- IAM
- IAM Credentials
- Logging
- Service Usage
- Storage

Existing shared resources:

- Firestore database: `(default)` unless environment output says otherwise.
- GCS audio bucket: Terraform output `audio_bucket_name`.
- Cloud Tasks queue: Terraform output `cloud_tasks_queue`.
- Artifact Registry Docker repository: Terraform output
  `artifact_registry_repository`.
- Public API runtime service account: Terraform output
  `api_service_account_email`.
- Inference runtime service account: Terraform output
  `inference_service_account_email`.
- Cloud Tasks OIDC service account: Terraform output
  `cloud_tasks_service_account_email`.

Required IAM:

- Inference runtime service account needs `roles/datastore.user` on the project.
- Inference runtime service account needs `roles/storage.objectAdmin` on the
  audio bucket.
- Cloud Tasks OIDC service account needs `roles/run.invoker` on the private
  inference Cloud Run service.
- Public API runtime service account needs `roles/run.invoker` on the private
  inference Cloud Run service so it can call `GET /internal/model-catalog`.
- If the public API mints identity tokens through a service account other than
  its own runtime identity, grant only the minimum service-account token
  permissions required by that chosen implementation. Prefer using the Cloud Run
  metadata server with the public API runtime identity when possible.

Cloud Run requirements:

- Deploy inference as a private service with unauthenticated access disabled.
- Configure GPU, CPU, memory, timeout, and concurrency around model needs.
- Keep public API CPU-only and independently scalable.
- Set lower concurrency for inference if model/GPU execution is not safe under
  parallel requests.
- Use the inference runtime service account, not user credentials or static key
  files.

## Environment Variables

Recommended environment variables:

```txt
APP_ENV=dev
APP_RUNTIME_MODE=cloud
APP_NAME=declip-inference-server
APP_VERSION=0.1.0
LOG_LEVEL=INFO
GCP_PROJECT_ID=
FIRESTORE_DATABASE=(default)
GCS_BUCKET_NAME=
CLOUD_TASKS_SERVICE_ACCOUNT=
INFERENCE_SERVICE_AUDIENCE=
ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS=
MODEL_CONFIG_PATH=config/models.yaml
APP_CONFIG_PATH=config/app.yaml
MODEL_ARTIFACT_CACHE_DIR=/tmp/declip-models
INFERENCE_DEVICE=cuda
```

Notes:

- `CLOUD_TASKS_SERVICE_ACCOUNT` is the only allowed caller for
  `POST /tasks/process-job`.
- `ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS` should include the public API
  runtime service account for `GET /internal/model-catalog`.
- `INFERENCE_SERVICE_AUDIENCE` should match the audience used by callers when
  minting OIDC tokens. If unset, use the Cloud Run service URL consistently.
- Do not use `.env` files, service account JSON, private keys, or generated
  tokens in committed code.

## Authentication For Private Endpoints

Implement Google OIDC token verification behind a `TokenVerifier` or
`ServiceTokenVerifier` boundary.

For Cloud Tasks:

- Verify the bearer token with `google.oauth2.id_token.verify_oauth2_token`.
- Validate the configured audience.
- Require the email claim to equal `CLOUD_TASKS_SERVICE_ACCOUNT`.
- Reject missing, malformed, invalid, expired, or wrong-audience tokens.

For internal model catalog calls:

- Use the same OIDC verification mechanism.
- Validate the email claim against
  `ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS`.
- Initially this should be the public API Cloud Run service account.

Do not use Auth0 for private inference endpoints. Auth0 is for frontend to
public API authentication only.

## Observability

Use structured JSON logs in cloud mode.

Minimum fields:

- request ID
- trace ID when present
- method
- path
- status code
- latency
- job ID when processing a job
- model family and concrete model metadata when safe
- failure code

Do not log:

- bearer tokens
- signed URLs
- raw user audio data
- full private artifact paths unless restricted to debug logs and safe
  environments
- stack traces in user-facing responses

Add timings for:

- model artifact load/cache
- input download
- audio probing/conversion
- per-channel inference
- output upload
- Firestore updates

## Testing Strategy

The default test suite must not require live Google Cloud, Cloud Tasks, GPUs,
ffmpeg, or model artifacts.

Use fakes for:

- Firestore repository
- GCS storage service
- task token verifier
- internal service token verifier
- audio probe/conversion service
- model runtime
- quota service where appropriate

Recommended tests:

- Config validation rejects duplicate families and duplicate sample rates.
- `GET /internal/model-catalog` omits artifact URIs and disabled models.
- `GET /internal/model-catalog` rejects missing or wrong service tokens.
- `POST /tasks/process-job` rejects missing or wrong task tokens.
- Processing rejects ownership mismatch.
- Processing is idempotent for terminal jobs.
- Processing transitions queued jobs to processing then succeeded.
- Permanent inference failure transitions jobs to failed and releases quota.
- Successful processing consumes reserved quota exactly once.
- Unsupported sample rate fails permanently.
- Audio conversion failures produce user-safe failure codes.
- Storage and database dependency failures produce retryable errors where
  appropriate.

Add opt-in smoke tests later, guarded by explicit variables such as
`RUN_GCP_SMOKE_TESTS=1`.

## Implementation Roadmap

### Phase 1. Skeleton Service

- Create FastAPI app factory.
- Add health and version endpoints.
- Add shared error response handling.
- Add structured request ID middleware.
- Add settings parsing from environment.
- Add local fake service wiring.
- Add tests for health, errors, and settings.

### Phase 2. Model Catalog

- Add typed private model config schema.
- Add startup validation.
- Add model registry and sample-rate resolution.
- Add sanitized catalog projection.
- Add private `GET /internal/model-catalog`.
- Add OIDC verifier boundary with fake tests.
- Ensure artifact URIs never appear in the sanitized response.

### Phase 3. Shared Cloud Boundaries

- Add Firestore job and quota repository interfaces.
- Add GCS storage interface.
- Add fake implementations for tests.
- Add Google implementations for cloud mode.
- Match the public API's job and quota field names.

### Phase 4. Task Processing

- Add `POST /tasks/process-job`.
- Add Cloud Tasks OIDC token verification.
- Implement job ownership validation.
- Implement safe job status transitions.
- Implement idempotent terminal-state handling.
- Add fake inference processor that creates deterministic output metadata.

### Phase 5. Audio Runtime

- Add ffprobe metadata probing boundary.
- Add ffmpeg conversion boundary.
- Add model-ready WAV generation.
- Add channel split and recombination behavior.
- Keep subprocess and filesystem behavior testable.

### Phase 6. Model Runtime

- Add artifact fetch/cache strategy.
- Add TorchScript model runner.
- Add `DeclippingModel` interface.
- Replace fake output behavior with real model processing.
- Validate GPU device behavior and model load lifecycle.

### Phase 7. Deployment

- Add Dockerfile suitable for Cloud Run GPU runtime.
- Document build and deploy commands using the shared Artifact Registry.
- Configure the private Cloud Run service.
- Grant `roles/run.invoker` to Cloud Tasks OIDC service account.
- Grant `roles/run.invoker` to the public API service account.
- Configure the public API with `INFERENCE_SERVICE_URL` and
  `INFERENCE_SERVICE_AUDIENCE`.

### Phase 8. Staging Smoke

- Add explicit smoke checklist or script.
- Confirm public API can call private `GET /internal/model-catalog`.
- Confirm upload confirmation enqueues Cloud Task to inference service.
- Confirm task token verification rejects unauthenticated callers.
- Confirm queued job can complete end to end.
- Confirm output object exists and job metadata is updated.

## Public API Integration Contract

The public API service is expected to:

- Treat the inference service as the model catalog source of truth.
- Call private `GET /internal/model-catalog` through a service boundary.
- Expose a public frontend-safe `GET /models` endpoint from the sanitized
  catalog.
- Validate `POST /jobs` model family from that catalog.
- Validate upload-confirm sample rate from that catalog.
- Continue enqueueing Cloud Tasks to `POST /tasks/process-job`.
- Keep frontend traffic pointed only at the public API.

The Cloud Task payload should remain job-oriented:

```json
{
  "job_id": "job_opaque_id",
  "user_id": "auth0_subject",
  "attempt": 1,
  "request_id": "req_...",
  "trace_id": "trace-optional"
}
```

Do not put model artifact URIs, signed URLs, or raw storage paths into task
payloads unless there is a concrete reason. The inference service can read all
required private fields from Firestore.

## Design Decisions To Preserve

- The inference service is private.
- The frontend never calls inference directly.
- The public API remains the frontend-facing API.
- Model config source of truth lives in the inference repo.
- The public API gets only a sanitized catalog through
  `GET /internal/model-catalog`.
- Cloud Tasks invokes job processing asynchronously.
- Default tests use fakes and do not hit live GCP.
- Config is validated at startup.
- Secrets stay out of config files and source control.
- Job processing is idempotent because Cloud Tasks may retry.
- User-safe errors are returned to callers; internal details go to logs.
