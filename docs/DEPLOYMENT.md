# Initial Cloud Run Deployment

This service consumes canonical PCM prepared by the CPU conversion service.
In passthrough mode it validates private service auth, reads
`model-input.f32.wav`, writes `model-output.f32.wav`, and enqueues CPU final
output encoding. It does not run a declipping model yet.

## Prerequisites

The shared public API project must already provide:

- Firestore database.
- GCS audio bucket.
- Inference and conversion Cloud Tasks queues.
- Artifact Registry Docker repository.
- Public API runtime service account.
- Inference runtime service account.
- Cloud Tasks OIDC service account.

Required IAM:

- Inference runtime service account has `roles/datastore.user` on the project.
- Inference runtime service account has `roles/storage.objectAdmin` on the
  audio bucket.
- Inference runtime service account can enqueue tasks on the conversion queue
  and attach the configured Cloud Tasks OIDC service account.
- Cloud Tasks OIDC service account has `roles/run.invoker` on this Cloud Run
  service.
- Public API runtime service account has `roles/run.invoker` on this Cloud Run
  service.

These Cloud Run invoker IAM bindings are durable infrastructure policy and
should be managed by Terraform in the shared backend infrastructure repo. The
deploy script does not grant them.

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
CLOUD_TASKS_CONVERSION_QUEUE=<conversion-queue-name>
CLOUD_TASKS_LOCATION=<queue-location>
CLOUD_TASKS_SERVICE_ACCOUNT=<cloud-tasks-service-account-email>
CONVERSION_SERVICE_URL=<conversion-cloud-run-service-url>
CONVERSION_SERVICE_AUDIENCE=<conversion-cloud-run-service-url>
INFERENCE_SERVICE_AUDIENCE=<cloud-run-service-url>
ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS=["<public-api-runtime-service-account-email>"]
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

For dev, prefer the deploy script:

```bash
cp scripts/deploy-cloud-run-dev.env.example .env.deploy.dev
./scripts/deploy-cloud-run-dev.sh
```

The script builds and pushes the image, deploys private Cloud Run, and prints
the service URL. It loads `.env.deploy.dev` when present and sets all runtime
environment variables in the deploy call. The env template contains the dev
Terraform output values and deployed conversion service URL; keep the copied
file untracked.
If `INFERENCE_SERVICE_AUDIENCE` is not supplied, it reuses the existing Cloud
Run service URL. For a first deploy where no service URL exists yet, set
`INFERENCE_SERVICE_AUDIENCE` explicitly. Cloud Run invoker IAM should already
be managed by Terraform.

The manual commands below are equivalent and useful when debugging deployment
settings.

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
export CLOUD_TASKS_CONVERSION_QUEUE="declip-audio-conversion-dev"
export CLOUD_TASKS_LOCATION="${REGION}"
export CONVERSION_SERVICE_URL="<conversion-service-url>"
export CONVERSION_SERVICE_AUDIENCE="${CONVERSION_SERVICE_URL}"
export INFERENCE_SERVICE_AUDIENCE="<caller-oidc-audience>"
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
  --set-env-vars="APP_ENV=dev,APP_RUNTIME_MODE=cloud,APP_NAME=declip-inference-server,APP_VERSION=0.1.0,LOG_LEVEL=INFO,GCP_PROJECT_ID=${PROJECT_ID},FIRESTORE_DATABASE=(default),GCS_BUCKET_NAME=${AUDIO_BUCKET_NAME},CLOUD_TASKS_CONVERSION_QUEUE=${CLOUD_TASKS_CONVERSION_QUEUE},CLOUD_TASKS_LOCATION=${CLOUD_TASKS_LOCATION},CLOUD_TASKS_SERVICE_ACCOUNT=${CLOUD_TASKS_SERVICE_ACCOUNT},CONVERSION_SERVICE_URL=${CONVERSION_SERVICE_URL},CONVERSION_SERVICE_AUDIENCE=${CONVERSION_SERVICE_AUDIENCE},INFERENCE_SERVICE_AUDIENCE=${INFERENCE_SERVICE_AUDIENCE},ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS=[\"${PUBLIC_API_SERVICE_ACCOUNT}\"],MODEL_CONFIG_PATH=config/models.yaml,APP_CONFIG_PATH=config/app.yaml,MODEL_ARTIFACT_CACHE_DIR=/tmp/declip-models,INFERENCE_DEVICE=cpu,INFERENCE_BACKEND=passthrough,MAX_DECODED_DURATION_SECONDS=1200"
```

After deploy, get the actual service URL for callers and smoke tests:

```bash
export INFERENCE_SERVICE_URL="$(
  gcloud run services describe "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format='value(status.url)'
)"
```

If Terraform does not manage invoke permissions yet, the equivalent manual
commands are:

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
