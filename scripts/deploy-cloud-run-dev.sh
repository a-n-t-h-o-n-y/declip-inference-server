#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${DEPLOY_ENV_FILE:-.env.deploy.dev}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
  echo "Loaded deploy environment from ${ENV_FILE}"
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
REPOSITORY="${REPOSITORY:-declip-dev}"
SERVICE="${SERVICE:-declip-inference-dev}"
IMAGE_TAG="${IMAGE_TAG:-dev}"

APP_ENV="${APP_ENV:-dev}"
APP_RUNTIME_MODE="${APP_RUNTIME_MODE:-cloud}"
APP_NAME="${APP_NAME:-declip-inference-server}"
APP_VERSION="${APP_VERSION:-0.1.0}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

GCP_PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID}}"
FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-(default)}"
GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-}"
CLOUD_TASKS_CONVERSION_QUEUE="${CLOUD_TASKS_CONVERSION_QUEUE:-declip-audio-conversion-dev}"
CLOUD_TASKS_LOCATION="${CLOUD_TASKS_LOCATION:-${REGION}}"
CLOUD_TASKS_SERVICE_ACCOUNT="${CLOUD_TASKS_SERVICE_ACCOUNT:-declip-tasks-dev@${PROJECT_ID}.iam.gserviceaccount.com}"
CONVERSION_SERVICE_URL="${CONVERSION_SERVICE_URL:-}"
CONVERSION_SERVICE_AUDIENCE="${CONVERSION_SERVICE_AUDIENCE:-${CONVERSION_SERVICE_URL}}"
PUBLIC_API_SERVICE_ACCOUNT="${PUBLIC_API_SERVICE_ACCOUNT:-declip-api-dev@${PROJECT_ID}.iam.gserviceaccount.com}"
INFERENCE_SERVICE_ACCOUNT="${INFERENCE_SERVICE_ACCOUNT:-declip-inference-dev@${PROJECT_ID}.iam.gserviceaccount.com}"
INFERENCE_SERVICE_AUDIENCE="${INFERENCE_SERVICE_AUDIENCE:-}"

MODEL_CONFIG_PATH="${MODEL_CONFIG_PATH:-config/models.yaml}"
APP_CONFIG_PATH="${APP_CONFIG_PATH:-config/app.yaml}"
MODEL_ARTIFACT_CACHE_DIR="${MODEL_ARTIFACT_CACHE_DIR:-/tmp/declip-models}"
INFERENCE_DEVICE="${INFERENCE_DEVICE:-cpu}"
INFERENCE_BACKEND="${INFERENCE_BACKEND:-passthrough}"
MAX_DECODED_DURATION_SECONDS="${MAX_DECODED_DURATION_SECONDS:-1200}"

CLOUD_RUN_CPU="${CLOUD_RUN_CPU:-2}"
CLOUD_RUN_MEMORY="${CLOUD_RUN_MEMORY:-4Gi}"
CLOUD_RUN_TIMEOUT="${CLOUD_RUN_TIMEOUT:-3600}"
CLOUD_RUN_CONCURRENCY="${CLOUD_RUN_CONCURRENCY:-1}"

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "PROJECT_ID is required. Set PROJECT_ID or run: gcloud config set project <project-id>" >&2
  exit 1
fi

if [[ "${APP_RUNTIME_MODE}" == "cloud" ]]; then
  missing=()
  [[ -z "${GCS_BUCKET_NAME}" ]] && missing+=("GCS_BUCKET_NAME")
  [[ -z "${CLOUD_TASKS_CONVERSION_QUEUE}" ]] && missing+=("CLOUD_TASKS_CONVERSION_QUEUE")
  [[ -z "${CLOUD_TASKS_LOCATION}" ]] && missing+=("CLOUD_TASKS_LOCATION")
  [[ -z "${CLOUD_TASKS_SERVICE_ACCOUNT}" ]] && missing+=("CLOUD_TASKS_SERVICE_ACCOUNT")
  [[ -z "${CONVERSION_SERVICE_URL}" ]] && missing+=("CONVERSION_SERVICE_URL")
  [[ -z "${CONVERSION_SERVICE_AUDIENCE}" ]] && missing+=("CONVERSION_SERVICE_AUDIENCE")
  [[ -z "${PUBLIC_API_SERVICE_ACCOUNT}" ]] && missing+=("PUBLIC_API_SERVICE_ACCOUNT")
  [[ -z "${INFERENCE_SERVICE_ACCOUNT}" ]] && missing+=("INFERENCE_SERVICE_ACCOUNT")
  if (( ${#missing[@]} > 0 )); then
    printf 'Missing required cloud-mode env vars:\n' >&2
    printf '  - %s\n' "${missing[@]}" >&2
    printf '\nCopy scripts/deploy-cloud-run-dev.env.example to .env.deploy.dev, fill it, then rerun this script.\n' >&2
    exit 1
  fi
fi

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}:${IMAGE_TAG}"

echo "Deploying ${SERVICE} to private Cloud Run"
echo "Project: ${PROJECT_ID}"
echo "Region: ${REGION}"
echo "Image: ${IMAGE}"
echo "Inference service account: ${INFERENCE_SERVICE_ACCOUNT}"
echo "Cloud Tasks caller: ${CLOUD_TASKS_SERVICE_ACCOUNT}"
echo "Public API caller: ${PUBLIC_API_SERVICE_ACCOUNT}"

gcloud config set project "${PROJECT_ID}" >/dev/null

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

if ! gcloud artifacts repositories describe "${REPOSITORY}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" \
    --repository-format docker \
    --location "${REGION}" \
    --description "Declip container images"
fi

existing_service_url=""
if existing_service_url="$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format='value(status.url)' 2>/dev/null)"; then
  :
else
  existing_service_url=""
fi

INFERENCE_SERVICE_AUDIENCE="${INFERENCE_SERVICE_AUDIENCE:-${existing_service_url}}"
if [[ "${APP_RUNTIME_MODE}" == "cloud" && -z "${INFERENCE_SERVICE_AUDIENCE}" ]]; then
  echo "INFERENCE_SERVICE_AUDIENCE is required for a first deploy because the Cloud Run URL does not exist yet." >&2
  echo "Set it to the audience your callers will use, then rerun this script." >&2
  exit 1
fi

echo "Audience: ${INFERENCE_SERVICE_AUDIENCE}"

gcloud builds submit --tag "${IMAGE}" .

allowed_internal_caller_service_accounts="[\"${PUBLIC_API_SERVICE_ACCOUNT}\"]"

gcloud run deploy "${SERVICE}" \
  --project "${PROJECT_ID}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --no-allow-unauthenticated \
  --service-account "${INFERENCE_SERVICE_ACCOUNT}" \
  --cpu "${CLOUD_RUN_CPU}" \
  --memory "${CLOUD_RUN_MEMORY}" \
  --timeout "${CLOUD_RUN_TIMEOUT}" \
  --concurrency "${CLOUD_RUN_CONCURRENCY}" \
  --set-env-vars "^|^APP_ENV=${APP_ENV}|APP_RUNTIME_MODE=${APP_RUNTIME_MODE}|APP_NAME=${APP_NAME}|APP_VERSION=${APP_VERSION}|LOG_LEVEL=${LOG_LEVEL}|GCP_PROJECT_ID=${GCP_PROJECT_ID}|FIRESTORE_DATABASE=${FIRESTORE_DATABASE}|GCS_BUCKET_NAME=${GCS_BUCKET_NAME}|CLOUD_TASKS_CONVERSION_QUEUE=${CLOUD_TASKS_CONVERSION_QUEUE}|CLOUD_TASKS_LOCATION=${CLOUD_TASKS_LOCATION}|CLOUD_TASKS_SERVICE_ACCOUNT=${CLOUD_TASKS_SERVICE_ACCOUNT}|CONVERSION_SERVICE_URL=${CONVERSION_SERVICE_URL}|CONVERSION_SERVICE_AUDIENCE=${CONVERSION_SERVICE_AUDIENCE}|INFERENCE_SERVICE_AUDIENCE=${INFERENCE_SERVICE_AUDIENCE}|ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS=${allowed_internal_caller_service_accounts}|MODEL_CONFIG_PATH=${MODEL_CONFIG_PATH}|APP_CONFIG_PATH=${APP_CONFIG_PATH}|MODEL_ARTIFACT_CACHE_DIR=${MODEL_ARTIFACT_CACHE_DIR}|INFERENCE_DEVICE=${INFERENCE_DEVICE}|INFERENCE_BACKEND=${INFERENCE_BACKEND}|MAX_DECODED_DURATION_SECONDS=${MAX_DECODED_DURATION_SECONDS}"

SERVICE_URL="$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format='value(status.url)')"

echo
echo "Service URL: ${SERVICE_URL}"
echo "Audience: ${INFERENCE_SERVICE_AUDIENCE}"
echo
echo "If this is the first inference-service deploy, set in backend Terraform dev tfvars:"
printf '  inference_cloud_run_service_name = "%s"\n' "${SERVICE}"
echo "then run: terraform -chdir=../declip-backend-server/infra/terraform/envs/dev apply"
