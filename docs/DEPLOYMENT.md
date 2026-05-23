# Initial Cloud Run Deployment

This service is ready for a first GCP smoke test in passthrough mode. The first
deployment validates private service auth, model catalog discovery, Cloud Tasks
invocation, Firestore job/quota updates, GCS input/output movement, and audio
probing. It does not run a declipping model yet.

## Prerequisites

The shared public API project must already provide:

- Firestore database.
- GCS audio bucket.
- Cloud Tasks queue.
- Artifact Registry Docker repository.
- Public API runtime service account.
- Inference runtime service account.
- Cloud Tasks OIDC service account.

Required IAM:

- Inference runtime service account has `roles/datastore.user` on the project.
- Inference runtime service account has `roles/storage.objectAdmin` on the
  audio bucket.
- Cloud Tasks OIDC service account has `roles/run.invoker` on this Cloud Run
  service.
- Public API runtime service account has `roles/run.invoker` on this Cloud Run
  service.

## Required Environment

Set these on the inference Cloud Run service:

```txt
APP_ENV=dev
APP_RUNTIME_MODE=cloud
APP_NAME=declip-inference-server
APP_VERSION=0.1.0
LOG_LEVEL=INFO
GCP_PROJECT_ID=<project-id>
FIRESTORE_DATABASE=(default)
GCS_BUCKET_NAME=<audio-bucket-name>
CLOUD_TASKS_SERVICE_ACCOUNT=<cloud-tasks-service-account-email>
INFERENCE_SERVICE_AUDIENCE=<cloud-run-service-url>
ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS=<public-api-runtime-service-account-email>
MODEL_CONFIG_PATH=config/models.yaml
APP_CONFIG_PATH=config/app.yaml
MODEL_ARTIFACT_CACHE_DIR=/tmp/declip-models
INFERENCE_DEVICE=cpu
INFERENCE_BACKEND=passthrough
MAX_DECODED_DURATION_SECONDS=1200
```

For the initial passthrough deployment, `INFERENCE_DEVICE` is not used by the
runner. Use `cpu` until the model runtime lands.

`INFERENCE_SERVICE_AUDIENCE` should match the audience used by Cloud Tasks and
the public API when minting OIDC tokens. The simplest first smoke is to use the
Cloud Run service URL.

## Build And Deploy

Example variables:

```bash
export PROJECT_ID="declip-dev"
export REGION="us-central1"
export SERVICE_NAME="declip-inference-server"
export IMAGE_REPO="us-central1-docker.pkg.dev/${PROJECT_ID}/declip"
export IMAGE="${IMAGE_REPO}/${SERVICE_NAME}:$(git rev-parse --short HEAD)"
export INFERENCE_SERVICE_ACCOUNT="declip-inference@${PROJECT_ID}.iam.gserviceaccount.com"
export CLOUD_TASKS_SERVICE_ACCOUNT="declip-tasks@${PROJECT_ID}.iam.gserviceaccount.com"
export PUBLIC_API_SERVICE_ACCOUNT="declip-api@${PROJECT_ID}.iam.gserviceaccount.com"
export AUDIO_BUCKET_NAME="declip-audio-dev"
```

Build and push:

```bash
gcloud builds submit \
  --project="${PROJECT_ID}" \
  --tag="${IMAGE}"
```

Deploy as a private Cloud Run service:

```bash
gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --service-account="${INFERENCE_SERVICE_ACCOUNT}" \
  --no-allow-unauthenticated \
  --cpu=2 \
  --memory=4Gi \
  --timeout=3600 \
  --concurrency=1 \
  --set-env-vars="APP_ENV=dev,APP_RUNTIME_MODE=cloud,APP_NAME=declip-inference-server,APP_VERSION=0.1.0,LOG_LEVEL=INFO,GCP_PROJECT_ID=${PROJECT_ID},FIRESTORE_DATABASE=(default),GCS_BUCKET_NAME=${AUDIO_BUCKET_NAME},CLOUD_TASKS_SERVICE_ACCOUNT=${CLOUD_TASKS_SERVICE_ACCOUNT},INFERENCE_SERVICE_AUDIENCE=https://${SERVICE_NAME}-REPLACE.run.app,ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS=${PUBLIC_API_SERVICE_ACCOUNT},MODEL_CONFIG_PATH=config/models.yaml,APP_CONFIG_PATH=config/app.yaml,MODEL_ARTIFACT_CACHE_DIR=/tmp/declip-models,INFERENCE_DEVICE=cpu,INFERENCE_BACKEND=passthrough,MAX_DECODED_DURATION_SECONDS=1200"
```

After deploy, get the actual service URL:

```bash
export INFERENCE_SERVICE_URL="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format='value(status.url)'
)"
```

Then update the audience to the exact service URL:

```bash
gcloud run services update "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --set-env-vars="INFERENCE_SERVICE_AUDIENCE=${INFERENCE_SERVICE_URL}"
```

Grant invoke permissions:

```bash
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="serviceAccount:${CLOUD_TASKS_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker"

gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="serviceAccount:${PUBLIC_API_SERVICE_ACCOUNT}" \
  --role="roles/run.invoker"
```

Configure the public API with:

```txt
INFERENCE_SERVICE_URL=<inference-service-url>
INFERENCE_SERVICE_AUDIENCE=<same audience configured above>
```

## Notes

- The Docker image installs `ffmpeg`, which provides `ffprobe`.
- Keep unauthenticated Cloud Run access disabled. App-level `/health` is safe,
  but private Cloud Run IAM should still protect the service.
- Do not configure service account JSON keys. Use Cloud Run service identity and
  Google-issued OIDC tokens.
