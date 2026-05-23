from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_health_returns_safe_metadata() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/health", headers={"x-request-id": "req_test"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "app_name": "declip-inference-server",
        "environment": "dev",
        "version": "0.1.0",
    }
    assert response.headers["x-request-id"] == "req_test"


def test_version_returns_safe_metadata() -> None:
    client = TestClient(create_app(Settings(app_version="0.2.0")))

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json()["version"] == "0.2.0"
