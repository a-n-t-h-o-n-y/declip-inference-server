from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.domain import JobRecord


TASK_ACCOUNT = "tasks@example.iam.gserviceaccount.com"


def test_process_job_transitions_queued_job_to_succeeded() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(_job())
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 1},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )

    assert response.status_code == 200
    assert response.json() == {"job_id": "job_1", "status": "succeeded", "attempt": 1}
    stored = app.state.job_repository.get_job("job_1")
    assert stored.output_gcs_uri == "gs://local-fake-output/user_1/job_1/output.wav"
    assert stored.model_name == "ddd-v1-44k"
    assert app.state.quota_service.used_seconds_by_user == {"user_1": 21}


def test_process_job_is_idempotent_for_terminal_jobs() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(_job(status="succeeded"))
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 2},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )

    assert response.status_code == 200
    assert response.json() == {"job_id": "job_1", "status": "succeeded", "attempt": 2}
    assert app.state.quota_service.used_seconds_by_user == {}


def test_process_job_rejects_ownership_mismatch() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(_job())
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "other_user", "attempt": 1},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_unsupported_sample_rate_fails_permanently_and_releases_quota() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(_job(sample_rate=32000))
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 1},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )

    assert response.status_code == 200
    assert response.json() == {"job_id": "job_1", "status": "failed", "attempt": 1}
    stored = app.state.job_repository.get_job("job_1")
    assert stored.error_code == "unsupported_sample_rate"
    assert app.state.quota_service.released_seconds_by_user == {"user_1": 21}


def test_non_queued_job_returns_conflict() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(_job(status="uploaded"))
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 1},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_state_conflict"


def _job(status: str = "queued", sample_rate: int = 44100) -> JobRecord:
    return JobRecord(
        id="job_1",
        user_id="user_1",
        status=status,
        model_family="ddd-v1",
        input_gcs_uri="gs://bucket/input.wav",
        input_duration_seconds=10.1,
        input_sample_rate_hz=sample_rate,
        input_channels=2,
    )
