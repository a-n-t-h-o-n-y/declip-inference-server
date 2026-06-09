# Identity STFT Debug Model Deployment

This service consumes canonical PCM prepared by the CPU conversion service.
In identity-STFT mode it loads the exported TorchScript debug artifact, applies
STFT/iSTFT without declipping, writes `model-output.f32.wav`, and enqueues CPU
final output encoding.

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
- Inference runtime service account has read access to the model artifact
  bucket containing the configured identity `.pt` artifact.
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
INFERENCE_BACKEND=identity_stft
MAX_DECODED_DURATION_SECONDS=1200
```

The identity STFT debug runtime is CPU-only and accepts canonical `48000` Hz
audio. It validates pipeline integration, not declipping quality.

## Publish Identity Artifact

From `declip-model`, export and upload the artifact referenced by
`config/models.yaml`:

```bash
uv run python scripts/export_identity_stft.py --output-dir artifacts/identity-stft-v0/export-20260526
gcloud storage cp \
  artifacts/identity-stft-v0/export-20260526/identity-stft-v0-48khz-0.1.0.pt \
  gs://declip-models-dev/identity-stft-v0/48000/identity-stft-v0-48khz-0.1.0.pt
```

`INFERENCE_SERVICE_AUDIENCE` should match the audience used by Cloud Tasks and
the public API when minting OIDC tokens. The simplest first smoke is to use the
Cloud Run service URL.

## Build And Deploy

For dev, build and push the inference image from this repository:

```bash
cp scripts/deploy-cloud-run-dev.env.example .env.deploy.dev
./scripts/deploy-cloud-run-dev.sh
```

The script builds and pushes only the container image. Terraform in
`declip-backend-server` owns the Cloud Run service shape: GPU, CPU, memory,
runtime environment variables, service account, scaling, IAM, and traffic
configuration.

After the image is pushed, deploy the Cloud Run service through Terraform:

```bash
terraform -chdir=infra/terraform/envs/dev apply
```

Terraform only compares the configured image reference string. If
`inference_cloud_run_image` is already set to
`us-central1-docker.pkg.dev/declip-v2-dev/declip-dev/declip-inference-dev:dev`,
pushing a new image to the same mutable `:dev` tag will not produce a Terraform
plan diff. Cloud Run also keeps running the previously resolved image digest
until a new revision is deployed.

For deploys that Terraform can detect, use an immutable image reference. Set
`IMAGE_TAG` to a unique value, such as the current Git SHA, before running the
build script:

```bash
IMAGE_TAG="$(git rev-parse --short HEAD)" ./scripts/deploy-cloud-run-dev.sh
```

Then update `inference_cloud_run_image` in
`declip-backend-server/infra/terraform/envs/dev/terraform.tfvars` to that new
tag, or preferably to the pushed image digest, and run Terraform apply. The
changed reference causes Terraform to create a new Cloud Run revision.

The manual commands below are equivalent for debugging image build and push
settings.

Example variables:

```bash
export PROJECT_ID="declip-dev"
export REGION="us-central1"
export REPOSITORY="declip-dev"
export SERVICE="declip-inference-dev"
export IMAGE_TAG="$(git rev-parse --short HEAD)"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}:${IMAGE_TAG}"
```

Build and push:

```bash
gcloud config set project "${PROJECT_ID}"
gcloud services enable cloudbuild.googleapis.com artifactregistry.googleapis.com

gcloud artifacts repositories describe "${REPOSITORY}" \
  --project "${PROJECT_ID}" \
  --location "${REGION}" \
  || gcloud artifacts repositories create "${REPOSITORY}" \
    --project "${PROJECT_ID}" \
    --repository-format docker \
    --location "${REGION}" \
    --description "Declip container images"

GAR_ACCESS_TOKEN="$(gcloud auth print-access-token)"
gcloud builds submit \
  --project="${PROJECT_ID}" \
  --config cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_GAR_ACCESS_TOKEN=${GAR_ACCESS_TOKEN}" \
  .
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
