# GCP Smoke Test

Run this after the initial private Cloud Run deployment is live and invoke IAM
has been granted to the public API and Cloud Tasks service accounts.

The smoke test intentionally uses passthrough inference. Success means the
private service boundary works end to end; it does not validate declipping
quality.

## Automated Auth Smoke

The script checks:

- Required smoke environment variables are present.
- `/health` is callable with an identity token.
- Unauthenticated `/internal/model-catalog` is rejected.
- The public API service account can call `/internal/model-catalog`.
- The Cloud Tasks service account can call `/tasks/process-job`; a nonexistent
  job should return `404 job_not_found`, which proves auth passed before
  Firestore lookup.

Required local environment:

```txt
RUN_GCP_SMOKE_TESTS=1
GCP_PROJECT_ID=<project-id>
INFERENCE_SERVICE_URL=<cloud-run-service-url>
INFERENCE_SERVICE_AUDIENCE=<oidc-audience>
PUBLIC_API_SERVICE_ACCOUNT=<public-api-runtime-service-account-email>
CLOUD_TASKS_SERVICE_ACCOUNT=<cloud-tasks-service-account-email>
```

Run:

```bash
RUN_GCP_SMOKE_TESTS=1 \
GCP_PROJECT_ID="${PROJECT_ID}" \
INFERENCE_SERVICE_URL="${INFERENCE_SERVICE_URL}" \
INFERENCE_SERVICE_AUDIENCE="${INFERENCE_SERVICE_URL}" \
PUBLIC_API_SERVICE_ACCOUNT="${PUBLIC_API_SERVICE_ACCOUNT}" \
CLOUD_TASKS_SERVICE_ACCOUNT="${CLOUD_TASKS_SERVICE_ACCOUNT}" \
scripts/gcp_smoke.sh
```

The caller needs permission to impersonate the two service accounts if using
`gcloud auth print-identity-token --impersonate-service-account`.

## Manual End-To-End Passthrough Smoke

Use the public API path when possible:

1. Configure the public API with `INFERENCE_SERVICE_URL` and
   `INFERENCE_SERVICE_AUDIENCE`.
2. Create a job through the public API.
3. Upload a small valid WAV file.
4. Confirm upload so the public API validates audio metadata, reserves quota,
   and enqueues input conversion.
5. Confirm CPU conversion writes `model-input.f32.wav`, chooses
   `model_sample_rate_hz`, and enqueues `POST /tasks/process-job`.
6. Confirm the Firestore job becomes `processing/awaiting_output_encoding`.
7. Confirm `model_output_gcs_uri` exists in the audio bucket.
8. Confirm a conversion `/tasks/finalize-output` task was enqueued.
9. Let finalization run, then confirm the job is `succeeded` and quota moved
   from `audio_seconds_reserved` to `audio_seconds_used`.
10. Re-run the same task or wait for a retry and confirm no duplicate quota
   consumption occurs.

Expected passthrough behavior:

- `model-output.f32.wav` bytes match `model-input.f32.wav`.
- Output metadata includes `inference_backend=passthrough`.
- Inference-populated job fields include:
  - `model_name`
  - `model_version`
  - `model_sample_rate_hz`
  - `model_input_gcs_uri`
  - `model_output_gcs_uri`
  - `processing_codec`
  - `processing_sample_rate_hz`

## Troubleshooting

- `401` means token verification failed or the token audience is wrong.
- `403` means the token is valid but the service account is not configured as
  an allowed caller.
- `404 job_not_found` from `/tasks/process-job` is expected for the auth-only
  smoke payload.
- `503 database_unavailable` or `503 storage_unavailable` should be retried by
  Cloud Tasks.
- `failed` jobs with audio validation errors are permanent and should release
  reserved quota.
