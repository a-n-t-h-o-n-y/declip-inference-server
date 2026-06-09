# Inference Repository Agent Guide

This document is intended to be copied into the separate private GPU inference
repository. It describes the inference service after introducing the separate
CPU audio conversion service.

## Purpose

Build a private FastAPI Cloud Run GPU service that owns:

- The private model catalog and sanitized `GET /internal/model-catalog`.
- Concrete model artifact loading and GPU lifecycle management.
- Channel splitting, declipping inference, and channel recombination on
  canonical PCM audio.
- Writing generated canonical PCM output.
- Enqueueing CPU final output encoding.
- Dispatching the user's next waiting conversion job after a permanent
  inference failure releases capacity.

It must not own:

- User authentication, signed URLs, or browser-facing routes.
- Decoding user uploads, sample-rate conversion, final WAV/FLAC encoding, or
  output-format policy.
- Initial job creation or initial quota reservation.

## Pipeline Position

```txt
Public API -> conversion queue -> CPU conversion service
CPU conversion service -> inference queue -> GPU inference service
GPU inference service -> conversion queue -> CPU conversion service finalization
```

Input conversion has already stored `model_input_gcs_uri` as canonical
`pcm_f32le` WAV at `job.model_sample_rate_hz`. Inference writes newly generated
canonical PCM to `model_output_gcs_uri` and never creates the final user output.

Per-user dispatch capacity is stored in shared Firestore document
`dispatch_policies/default`, field `max_parallel_jobs_per_user`. Terraform
provisions the global policy with launch default `1`. If this service
dispatches waiting work after permanent failure, it must read the policy in
the same claim transaction and fail retryably if it is missing or invalid; do
not use a service-local fallback.

## Repository Shape

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
    database.py
    inference.py
    model_catalog.py
    model_runtime.py
    queue.py
    storage.py
    task_auth.py
    task_processing.py
config/
  app.yaml
  models.yaml
docs/
tests/
Dockerfile
pyproject.toml
```

Use the public backend's FastAPI/Pydantic/service-boundary patterns and `uv`
test workflow. Default tests must use fakes and not require Google Cloud, GPU
availability, or real artifacts.

## Endpoints

### `GET /health` And `GET /version`

Expose safe process/build metadata only. No bucket, model-artifact, or
credential details.

### `GET /internal/model-catalog`

Private endpoint invoked by the public API runtime identity. It is the source
of truth for frontend-safe family discovery:

```json
{
  "catalog_version": "0.1.0",
  "model_families": [
    {
      "family": "ddd-v1",
      "display_name": "DDD v1",
      "description": "General declipping model family.",
      "enabled": true,
      "supported_sample_rates_hz": [44100, 48000]
    }
  ]
}
```

Do not expose artifact URIs, filesystem paths, GPU constraints, or private
storage paths. The public API uses `supported_sample_rates_hz` to choose the
smallest model rate at or above the uploaded input rate; it rejects inputs
above all rates in the selected family.

### `POST /tasks/process-job`

Private Cloud Tasks endpoint:

```json
{
  "job_id": "job_opaque_id",
  "user_id": "auth0_subject",
  "attempt": 1,
  "request_id": "req_...",
  "trace_id": "trace-optional"
}
```

Behavior:

1. Verify Cloud Tasks OIDC identity and fetch the job.
2. Require `job.user_id` to match the payload.
3. Require `status=processing` and
   `processing_stage=awaiting_inference`, unless the task is already complete
   according to idempotency rules.
4. Transactionally claim `processing_stage=inferring`.
5. Download `model_input_gcs_uri`, require canonical PCM metadata, and resolve
   the concrete model by `model_family` and `model_sample_rate_hz`.
6. Split channels as needed, execute declipping for each channel, recombine
   with original layout, and retain canonical PCM representation.
7. Upload generated PCM to `model_output_gcs_uri`.
8. Store `model_name`, `model_version`, and safe inference metadata.
9. Transactionally set `processing_stage=awaiting_output_encoding`.
10. Enqueue `POST /tasks/finalize-output` on the CPU conversion service.

Successful response:

```json
{
  "job_id": "job_opaque_id",
  "status": "processing",
  "processing_stage": "awaiting_output_encoding",
  "attempt": 1
}
```

The conversion service, not inference, encodes the final output, consumes
reserved quota after output upload, and marks the job `succeeded`.

## Canonical PCM Contract

Input and output internal objects use:

```txt
container: wav
codec: pcm_f32le
sample rate: job.model_sample_rate_hz
channels: preserve source layout at service boundary
```

Object layout:

```txt
users/{uid}/jobs/{job_id}/working/model-input.f32.wav
users/{uid}/jobs/{job_id}/working/model-output.f32.wav
```

Inference must reject malformed input or an input rate that does not match the
planned model rate. It must not download the original upload for conversion or
encode WAV/FLAC public output.

## Model Runtime Interface

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


class DeclippingModel(Protocol):
    spec: ModelSpec

    def load(self, device: torch.device) -> None: ...

    def process_channel(self, request: InferenceInput) -> Path: ...
```

Model implementations must not read Firestore, read GCS, create tasks, verify
tokens, or know output-format policy.

## Private Model Configuration

Commit a typed non-secret `config/models.yaml` that includes concrete model
artifacts and enabled rates:

```yaml
catalog_version: 0.1.0
model_families:
  - family: ddd-v1
    enabled: true
    display_name: DDD v1
    description: General declipping model family.
    models:
      - sample_rate_hz: 44100
        model_name: ddd-v1-44k
        model_version: 1.0.0
        artifact_uri: gs://declip-models-dev/ddd-v1/44100/model.pt
        runtime: pytorch
        enabled: true
```

Fail startup for duplicate families, duplicate enabled rates, enabled families
without models, missing artifacts, or unsupported runtime configuration.
Initially support TorchScript `.pt` artifacts unless an explicit model decision
changes that.

## Shared Job Contract

Inference reads or updates:

```txt
id
user_id
status
processing_stage
model_family
model_name
model_version
model_sample_rate_hz
model_input_gcs_uri
model_output_gcs_uri
input_channels
processing_codec
processing_sample_rate_hz
error_code
error_message
updated_at
```

Never replace the shared job document with an inference-local model. Use
partial or transactional field updates only, preserving required public API
fields such as `created_at`, original input metadata, and output planning
fields owned by the public API/conversion service.

Public statuses remain:

```txt
queued -> processing -> succeeded | failed
```

This service operates only while public status is `processing`. The CPU
conversion service changes `queued` to `processing` after claiming input
conversion and changes `processing` to `succeeded` during finalization.

On a permanent inference error, inference may transactionally mark the job
`failed` and release the previously reserved quota exactly once. After that
terminal operation, it must dispatch the user's oldest
`queued`/`initial_dispatch_status=waiting` job if per-user capacity is now
available. Claim it as `pending`, enqueue its deterministic
`POST /tasks/convert-input` task on the conversion queue using task ID
`convert-input-` plus the first 32 lowercase hexadecimal characters of
`sha256(job_id)`, and record `enqueued`; retries reuse the same task identity
and treat an existing task as success. On transient storage, Firestore,
model-fetch, or downstream enqueue failure, return a retryable failure.

Permanent-failure dispatch must first resume any existing
`queued`/`initial_dispatch_status=pending` claim for the user. It must perform
that recovery even when a retry finds the original inference job already
`failed`, because failure accounting may have committed before the next task
was created. For a newly dispatched conversion task send `attempt=1`, generate
a fresh opaque `request_id` for each Cloud Tasks create attempt, and propagate
`trace_id` only when available. Task-name determinism, not `request_id`, is
the idempotency mechanism.

## Downstream Finalization Contract

After generated PCM has been safely stored and the stage transitioned, enqueue:

```http
POST {CONVERSION_SERVICE_URL}/tasks/finalize-output
```

Use `CLOUD_TASKS_CONVERSION_QUEUE`, Cloud Tasks OIDC identity, and
`CONVERSION_SERVICE_AUDIENCE`. Send only the standard identifier payload; the
conversion service reads storage/output policy from the job.

## Authentication And IAM

- `GET /internal/model-catalog` permits only configured internal caller service
  accounts, initially the public API runtime identity.
- `POST /tasks/process-job` permits only OIDC tokens from
  `CLOUD_TASKS_SERVICE_ACCOUNT` for the inference service audience.
- Do not use Auth0 in this private service.

Shared Terraform in the public backend provisions:

- Inference runtime service account.
- Inference Cloud Tasks queue.
- Firestore and GCS permissions for inference.
- Task-enqueue and task-service-account attachment permissions for inference.
- Conditional Cloud Run invoker bindings.

This repository builds and pushes the inference container image. Backend
Terraform owns the Cloud Run service shape, runtime environment, IAM, scaling,
and traffic configuration. Terraform only detects image changes when the
configured image reference changes, so deploys should use a unique tag or image
digest instead of repeatedly pushing to the same mutable tag.

## Environment Variables

```txt
APP_ENV=dev
APP_RUNTIME_MODE=cloud
APP_NAME=declip-inference-server
APP_VERSION=0.1.0
LOG_LEVEL=INFO
GCP_PROJECT_ID=
FIRESTORE_DATABASE=(default)
GCS_BUCKET_NAME=
CLOUD_TASKS_CONVERSION_QUEUE=
CLOUD_TASKS_LOCATION=
CLOUD_TASKS_SERVICE_ACCOUNT=
CONVERSION_SERVICE_URL=
CONVERSION_SERVICE_AUDIENCE=
INFERENCE_SERVICE_AUDIENCE=
ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS=
MODEL_CONFIG_PATH=config/models.yaml
APP_CONFIG_PATH=config/app.yaml
MODEL_ARTIFACT_CACHE_DIR=/tmp/declip-models
INFERENCE_DEVICE=cuda
```

## Testing And Implementation Order

Required default tests:

- Typed model config validation and sanitized catalog projection.
- Internal catalog OIDC caller validation.
- Task OIDC validation and job ownership checks.
- Canonical PCM input validation.
- Concrete model resolution using the preselected job sample rate.
- Stage claim and task retry idempotency.
- Generated PCM upload and one finalization enqueue.
- Permanent failure and exactly-once reserved quota release.
- Transient dependency retry behavior.

Recommended phases:

1. FastAPI skeleton, settings, errors, logging, health/version, and fakes.
2. Private sanitized model catalog and internal OIDC endpoint.
3. Firestore, GCS, queue, and task OIDC boundaries.
4. Canonical PCM task processing with fake model execution.
5. TorchScript/GPU runtime and artifact cache.
6. Private Cloud Run deployment and end-to-end dev smoke with the conversion
   service.

## Decisions To Preserve

- GPU inference is a private independently deployed service.
- The inference repository remains model-catalog source of truth.
- The CPU conversion service performs all media decode, resample, and final
  output encoding work.
- Inference consumes and emits canonical PCM only.
- Intermediate PCM is never public.
- Default tests are deterministic and cloud-free.
