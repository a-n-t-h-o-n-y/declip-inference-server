from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = Field(default="dev", alias="APP_ENV")
    app_runtime_mode: str = Field(default="local", alias="APP_RUNTIME_MODE")
    app_name: str = Field(default="declip-inference-server", alias="APP_NAME")
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    gcp_project_id: str | None = Field(default=None, alias="GCP_PROJECT_ID")
    firestore_database: str = Field(default="(default)", alias="FIRESTORE_DATABASE")
    gcs_bucket_name: str | None = Field(default=None, alias="GCS_BUCKET_NAME")
    cloud_tasks_service_account: str | None = Field(
        default=None, alias="CLOUD_TASKS_SERVICE_ACCOUNT"
    )
    inference_service_audience: str | None = Field(default=None, alias="INFERENCE_SERVICE_AUDIENCE")
    allowed_internal_caller_service_accounts: list[str] = Field(
        default_factory=list, alias="ALLOWED_INTERNAL_CALLER_SERVICE_ACCOUNTS"
    )
    model_config_path: Path = Field(default=Path("config/models.yaml"), alias="MODEL_CONFIG_PATH")
    app_config_path: Path = Field(default=Path("config/app.yaml"), alias="APP_CONFIG_PATH")
    model_artifact_cache_dir: Path = Field(
        default=Path("/tmp/declip-models"), alias="MODEL_ARTIFACT_CACHE_DIR"
    )
    inference_device: str = Field(default="cuda", alias="INFERENCE_DEVICE")

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    @field_validator("app_runtime_mode")
    @classmethod
    def validate_runtime_mode(cls, value: str) -> str:
        if value not in {"local", "cloud"}:
            raise ValueError("APP_RUNTIME_MODE must be local or cloud")
        return value

    @field_validator("allowed_internal_caller_service_accounts", mode="before")
    @classmethod
    def split_service_accounts(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
