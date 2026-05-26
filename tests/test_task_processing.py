from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.domain import JobRecord


TASK_ACCOUNT = "tasks@example.iam.gserviceaccount.com"


def test_process_job_writes_model_output_stage_and_enqueues_finalization() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(_job())
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 1},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "job_id": "job_1",
        "status": "processing",
        "processing_stage": "awaiting_output_encoding",
        "attempt": 1,
    }
    stored = app.state.job_repository.get_job("job_1")
    assert stored.model_output_gcs_uri == "gs://bucket/working/model-output.f32.wav"
    assert stored.model_name == "ddd-v1-44k"
    assert stored.processing_codec == "pcm_f32le"
    assert [task.job_id for task in app.state.finalization_queue.payloads] == ["job_1"]


def test_process_job_is_idempotent_for_terminal_jobs() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(
        _job(status="succeeded", processing_stage="awaiting_output_encoding")
    )
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 2},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "job_id": "job_1",
        "status": "succeeded",
        "processing_stage": "awaiting_output_encoding",
        "attempt": 2,
    }


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
    assert response.json() == {
        "job_id": "job_1",
        "status": "failed",
        "processing_stage": "inferring",
        "attempt": 1,
    }
    stored = app.state.job_repository.get_job("job_1")
    assert stored.error_code == "unsupported_sample_rate"
    assert app.state.quota_service.released_seconds_by_user == {"user_1": 21}

    retry = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 2},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )
    assert retry.status_code == 200
    assert app.state.quota_service.released_seconds_by_user == {"user_1": 21}


def test_non_processing_job_returns_conflict() -> None:
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


def test_retry_at_output_encoding_stage_enqueues_at_most_one_finalization_task() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(_job(processing_stage="awaiting_output_encoding"))
    client = TestClient(app)

    for attempt in (1, 2):
        response = client.post(
            "/tasks/process-job",
            json={"job_id": "job_1", "user_id": "user_1", "attempt": attempt},
            headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
        )
        assert response.status_code == 200

    assert len(app.state.finalization_queue.payloads) == 1


def test_retry_while_inferring_completes_output_handoff() -> None:
    app = create_app(Settings(cloud_tasks_service_account=TASK_ACCOUNT))
    app.state.job_repository.add(_job(processing_stage="inferring"))
    client = TestClient(app)

    response = client.post(
        "/tasks/process-job",
        json={"job_id": "job_1", "user_id": "user_1", "attempt": 2},
        headers={"authorization": f"Bearer {TASK_ACCOUNT}"},
    )

    assert response.status_code == 200
    assert response.json()["processing_stage"] == "awaiting_output_encoding"
    assert len(app.state.finalization_queue.payloads) == 1


def _job(
    status: str = "processing",
    sample_rate: int = 44100,
    processing_stage: str = "awaiting_inference",
) -> JobRecord:
    return JobRecord(
        id="job_1",
        user_id="user_1",
        status=status,
        model_family="ddd-v1",
        input_gcs_uri=None,
        input_duration_seconds=10.1,
        input_sample_rate_hz=sample_rate,
        input_channels=2,
        processing_stage=processing_stage,
        model_input_gcs_uri="gs://bucket/working/model-input.f32.wav",
        model_output_gcs_uri="gs://bucket/working/model-output.f32.wav",
        model_sample_rate_hz=sample_rate,
    )
