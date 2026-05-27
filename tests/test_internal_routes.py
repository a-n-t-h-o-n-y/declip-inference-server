from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_internal_catalog_requires_token() -> None:
    client = TestClient(
        create_app(
            Settings(
                allowed_internal_caller_service_accounts=["api-runtime@example.iam.gserviceaccount.com"]
            )
        )
    )

    response = client.get("/internal/model-catalog")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_service_token"


def test_internal_catalog_rejects_forbidden_service_account() -> None:
    client = TestClient(
        create_app(
            Settings(
                allowed_internal_caller_service_accounts=["api-runtime@example.iam.gserviceaccount.com"]
            )
        )
    )

    response = client.get(
        "/internal/model-catalog",
        headers={"authorization": "Bearer other@example.iam.gserviceaccount.com"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_service_account"


def test_internal_catalog_returns_sanitized_catalog() -> None:
    client = TestClient(
        create_app(
            Settings(
                allowed_internal_caller_service_accounts=["api-runtime@example.iam.gserviceaccount.com"]
            )
        )
    )

    response = client.get(
        "/internal/model-catalog",
        headers={"authorization": "Bearer api-runtime@example.iam.gserviceaccount.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model_families"][0]["supported_sample_rates_hz"] == [48000]
    assert "artifact_uri" not in str(body)


def test_task_endpoint_requires_task_service_account() -> None:
    client = TestClient(
        create_app(Settings(cloud_tasks_service_account="tasks@example.iam.gserviceaccount.com"))
    )

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 1},
        headers={"authorization": "Bearer api-runtime@example.iam.gserviceaccount.com"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_service_account"


def test_task_endpoint_accepts_local_fake_task_token_then_reports_missing_job() -> None:
    client = TestClient(
        create_app(Settings(cloud_tasks_service_account="tasks@example.iam.gserviceaccount.com"))
    )

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 1},
        headers={"authorization": "Bearer tasks@example.iam.gserviceaccount.com"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"
