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

PROJECT_ID="${PROJECT_ID:-declip-v2-dev}"
REGION="${REGION:-us-central1}"
REPOSITORY="${REPOSITORY:-declip-dev}"
SERVICE="${SERVICE:-declip-inference-dev}"
IMAGE_TAG="${IMAGE_TAG:-dev}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}:${IMAGE_TAG}"

echo "Building inference image"
echo "Project: ${PROJECT_ID}"
echo "Region: ${REGION}"
echo "Repository: ${REPOSITORY}"
echo "Image: ${IMAGE}"

gcloud config set project "${PROJECT_ID}" >/dev/null

gcloud services enable \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

if ! gcloud artifacts repositories describe "${REPOSITORY}" \
  --project "${PROJECT_ID}" \
  --location "${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPOSITORY}" \
    --project "${PROJECT_ID}" \
    --repository-format docker \
    --location "${REGION}" \
    --description "Declip container images"
fi

GAR_ACCESS_TOKEN="$(gcloud auth print-access-token)"
gcloud builds submit \
  --project "${PROJECT_ID}" \
  --config cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_GAR_ACCESS_TOKEN=${GAR_ACCESS_TOKEN}" \
  .

echo
echo "Image pushed:"
echo "  ${IMAGE}"
echo
echo "Now deploy through Terraform from declip-backend-server:"
echo "  terraform -chdir=infra/terraform/envs/dev apply"
