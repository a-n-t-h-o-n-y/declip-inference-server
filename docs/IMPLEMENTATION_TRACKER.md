# Inference Server Implementation Tracker

This tracker is the working checklist for implementing the private Declip
inference service. Update it as each piece lands so the current state is visible
without rereading the full agent guide.

## Current Deploy Target

Initial deploy should be usable without model execution:

- Validate private service auth.
- Return sanitized model catalog to the public API.
- Accept Cloud Tasks job payloads.
- Read inference-ready Firestore jobs at `processing/awaiting_inference`.
- Download and validate canonical `model-input.f32.wav` from GCS.
- Upload canonical `model-output.f32.wav` through passthrough inference.
- Advance to `awaiting_output_encoding` and enqueue CPU finalization.
- Mark permanent inference failures `failed` with user-safe errors.

Real declipping model loading and channel processing will replace the
passthrough runner later behind the same inference interface.

## Completed

- [x] Scaffold FastAPI app factory and route layout.
- [x] Add `/health` and `/version`.
- [x] Add shared error response shape with request IDs.
- [x] Add settings parsing from environment.
- [x] Add model catalog config at `config/models.yaml`.
- [x] Validate model catalog uniqueness and enabled model rules.
- [x] Add sanitized `GET /internal/model-catalog`.
- [x] Add service-token verifier boundary.
- [x] Add local fake token verifier for tests/dev.
- [x] Add `POST /tasks/process-job` route.
- [x] Add task processor service boundary.
- [x] Add idempotent task behavior for terminal/output-encoding jobs.
- [x] Add ownership mismatch handling.
- [x] Add preselected model-rate permanent failure handling.
- [x] Add in-memory job/quota/storage fakes for tests.
- [x] Add Firestore job repository.
- [x] Add Firestore quota release service for permanent inference failures.
- [x] Add GCS storage service.
- [x] Add passthrough inference runner for initial deploy.
- [x] Add audio probe interface and `ffprobe` implementation.
- [x] Add canonical PCM duration/sample-rate/channel/codec validation.
- [x] Add conversion finalization queue boundary and Cloud Tasks implementation.
- [x] Add focused tests that do not require GCP, GPU, model artifacts, or
  `ffmpeg`.

## Remaining Before First GCP Smoke

- [x] Add Firestore/GCS dependency error mapping so transient failures return
  retryable `503` responses instead of generic `500`.
- [x] Confirm Firestore field names match the public API job/quota documents.
- [x] Confirm inference does not consume quota before CPU output finalization.
- [x] Add Docker runtime packages for audio probing, especially `ffmpeg`
  / `ffprobe`.
- [x] Add deployment docs with required environment variables and Cloud Run
  settings.
- [x] Add a GCP smoke checklist or script guarded by explicit environment
  variables.

## First GCP Smoke Test

Run the first GCP test after the "Remaining Before First GCP Smoke" checklist is
done. Do not wait for real model runtime work; passthrough inference is enough
to validate the service boundary.

Minimum smoke scope:

- [ ] Deploy private Cloud Run service with unauthenticated access disabled.
- [ ] Confirm `/health` returns safe metadata.
- [ ] Confirm unauthenticated `/internal/model-catalog` is rejected.
- [ ] Confirm the public API service account can call
  `/internal/model-catalog`.
- [ ] Confirm the Cloud Tasks service account can call
  `/tasks/process-job`.
- [ ] Seed one `processing/awaiting_inference` job with canonical PCM in GCS.
- [ ] Process that job through Cloud Tasks.
- [ ] Confirm `model-output.f32.wav` exists and matches passthrough behavior.
- [ ] Confirm Firestore stage becomes `awaiting_output_encoding`.
- [ ] Confirm one `/tasks/finalize-output` conversion task is enqueued.
- [ ] Confirm retrying the same task is idempotent.

## Remaining After First GCP Smoke

- [ ] Add channel splitting and recombination.
- [ ] Add model artifact download/cache strategy.
- [ ] Add TorchScript model runtime implementation.
- [ ] Add GPU device selection and model load lifecycle.
- [ ] Replace passthrough inference with real declipping model processing.
- [ ] Add broader model-runtime tests with fakes.
- [ ] Add opt-in live GCP smoke tests.
- [ ] Add production operations docs.
- [ ] Add architecture, API contract, inference, and testing docs.

## Useful Commit Messages

- `Map cloud dependency failures to retryable errors`
- `Document initial Cloud Run deployment`
- `Add GCP smoke checklist`
- `Add ffmpeg conversion boundary`
- `Add model artifact cache and runtime interface`
- `Wire TorchScript declipping runtime`
