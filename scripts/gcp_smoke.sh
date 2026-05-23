#!/usr/bin/env bash
set -euo pipefail

if [[ "${RUN_GCP_SMOKE_TESTS:-}" != "1" ]]; then
  echo "Set RUN_GCP_SMOKE_TESTS=1 to run GCP smoke checks."
  exit 0
fi

required_vars=(
  GCP_PROJECT_ID
  INFERENCE_SERVICE_URL
  INFERENCE_SERVICE_AUDIENCE
  PUBLIC_API_SERVICE_ACCOUNT
  CLOUD_TASKS_SERVICE_ACCOUNT
)

for var_name in "${required_vars[@]}"; do
  if [[ -z "${!var_name:-}" ]]; then
    echo "Missing required environment variable: ${var_name}" >&2
    exit 2
  fi
done

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

curl_status() {
  local method="$1"
  local url="$2"
  local token="${3:-}"
  local body="${4:-}"
  local output_path="$5"

  if [[ -n "${token}" && -n "${body}" ]]; then
    curl -sS -o "${output_path}" -w "%{http_code}" \
      -X "${method}" \
      -H "Authorization: Bearer ${token}" \
      -H "Content-Type: application/json" \
      --data "${body}" \
      "${url}"
  elif [[ -n "${token}" ]]; then
    curl -sS -o "${output_path}" -w "%{http_code}" \
      -X "${method}" \
      -H "Authorization: Bearer ${token}" \
      "${url}"
  else
    curl -sS -o "${output_path}" -w "%{http_code}" \
      -X "${method}" \
      "${url}"
  fi
}

identity_token() {
  local service_account="$1"
  gcloud auth print-identity-token \
    --project="${GCP_PROJECT_ID}" \
    --audiences="${INFERENCE_SERVICE_AUDIENCE}" \
    --impersonate-service-account="${service_account}"
}

expect_status() {
  local actual="$1"
  local expected="$2"
  local label="$3"
  local body_path="$4"

  if [[ "${actual}" != "${expected}" ]]; then
    echo "${label} failed: expected HTTP ${expected}, got ${actual}" >&2
    echo "Response body:" >&2
    cat "${body_path}" >&2
    echo >&2
    exit 1
  fi

  echo "${label}: HTTP ${actual}"
}

echo "Minting public API caller token..."
public_api_token="$(identity_token "${PUBLIC_API_SERVICE_ACCOUNT}")"

echo "Minting Cloud Tasks caller token..."
tasks_token="$(identity_token "${CLOUD_TASKS_SERVICE_ACCOUNT}")"

health_body="${tmp_dir}/health.json"
health_status="$(curl_status GET "${INFERENCE_SERVICE_URL}/health" "${public_api_token}" "" "${health_body}")"
expect_status "${health_status}" "200" "health" "${health_body}"

unauth_catalog_body="${tmp_dir}/catalog-unauth.json"
unauth_catalog_status="$(curl_status GET "${INFERENCE_SERVICE_URL}/internal/model-catalog" "" "" "${unauth_catalog_body}")"
if [[ "${unauth_catalog_status}" == "200" ]]; then
  echo "unauthenticated model catalog failed: expected rejection, got HTTP 200" >&2
  cat "${unauth_catalog_body}" >&2
  echo >&2
  exit 1
fi
echo "unauthenticated model catalog rejected: HTTP ${unauth_catalog_status}"

catalog_body="${tmp_dir}/catalog.json"
catalog_status="$(curl_status GET "${INFERENCE_SERVICE_URL}/internal/model-catalog" "${public_api_token}" "" "${catalog_body}")"
expect_status "${catalog_status}" "200" "authenticated model catalog" "${catalog_body}"
if grep -q "artifact_uri" "${catalog_body}"; then
  echo "authenticated model catalog leaked artifact_uri" >&2
  cat "${catalog_body}" >&2
  echo >&2
  exit 1
fi
echo "authenticated model catalog is sanitized"

task_body="${tmp_dir}/task.json"
task_payload='{"job_id":"job_smoke_missing","user_id":"smoke_user","attempt":1,"request_id":"req_gcp_smoke"}'
task_status="$(curl_status POST "${INFERENCE_SERVICE_URL}/tasks/process-job" "${tasks_token}" "${task_payload}" "${task_body}")"
expect_status "${task_status}" "404" "task auth and missing-job lookup" "${task_body}"
if ! grep -q "job_not_found" "${task_body}"; then
  echo "task smoke expected job_not_found response" >&2
  cat "${task_body}" >&2
  echo >&2
  exit 1
fi

echo "GCP auth smoke checks passed."
