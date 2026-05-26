from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.api.routes.internal import router as internal_router
from app.api.routes.tasks import router as tasks_router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.services.audio import FfprobeAudioProbeService
from app.services.database import FirestoreJobRepository, InMemoryJobRepository
from app.services.inference import PassthroughInferenceRunner
from app.services.model_catalog import ModelCatalog
from app.services.quotas import FirestoreQuotaService, InMemoryQuotaService
from app.services.queue import CloudTasksFinalizationQueue, InMemoryFinalizationQueue
from app.services.storage import GcsStorageService, InMemoryStorageService
from app.services.task_auth import FakeServiceTokenVerifier, GoogleServiceTokenVerifier
from app.services.task_processing import FakeInferenceRunner, TaskProcessor


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    catalog = ModelCatalog.from_path(settings.model_config_path)
    token_verifier = (
        FakeServiceTokenVerifier()
        if settings.app_runtime_mode == "local"
        else GoogleServiceTokenVerifier(settings.inference_service_audience)
    )

    app = FastAPI(title=settings.app_name, version=settings.app_version)
    app.state.settings = settings
    app.state.model_catalog = catalog
    app.state.token_verifier = token_verifier
    if settings.app_runtime_mode == "local":
        app.state.job_repository = InMemoryJobRepository()
        app.state.quota_service = InMemoryQuotaService()
        app.state.storage_service = InMemoryStorageService()
        app.state.finalization_queue = InMemoryFinalizationQueue()
        inference = FakeInferenceRunner()
    else:
        required_queue_settings = {
            "GCP_PROJECT_ID": settings.gcp_project_id,
            "CLOUD_TASKS_LOCATION": settings.cloud_tasks_location,
            "CLOUD_TASKS_CONVERSION_QUEUE": settings.cloud_tasks_conversion_queue,
            "CONVERSION_SERVICE_URL": settings.conversion_service_url,
            "CLOUD_TASKS_SERVICE_ACCOUNT": settings.cloud_tasks_service_account,
            "CONVERSION_SERVICE_AUDIENCE": settings.conversion_service_audience,
        }
        missing = [name for name, value in required_queue_settings.items() if not value]
        if missing:
            raise ValueError(f"Missing cloud finalization queue settings: {', '.join(missing)}")
        app.state.job_repository = FirestoreJobRepository(
            project_id=settings.gcp_project_id,
            database=settings.firestore_database,
        )
        app.state.quota_service = FirestoreQuotaService(
            project_id=settings.gcp_project_id,
            database=settings.firestore_database,
        )
        app.state.storage_service = GcsStorageService(project_id=settings.gcp_project_id)
        app.state.audio_probe_service = FfprobeAudioProbeService()
        app.state.finalization_queue = CloudTasksFinalizationQueue(
            project_id=settings.gcp_project_id,
            location=settings.cloud_tasks_location,
            queue_name=settings.cloud_tasks_conversion_queue,
            service_url=settings.conversion_service_url,
            service_account_email=settings.cloud_tasks_service_account,
            audience=settings.conversion_service_audience,
        )
        inference = PassthroughInferenceRunner(
            storage=app.state.storage_service,
            audio_probe=app.state.audio_probe_service,
            max_duration_seconds=settings.max_decoded_duration_seconds,
        )

    app.state.task_processor = TaskProcessor(
        jobs=app.state.job_repository,
        quotas=app.state.quota_service,
        catalog=catalog,
        inference=inference,
        finalization_queue=app.state.finalization_queue,
    )

    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(internal_router)
    app.include_router(tasks_router)
    return app


app = create_app()
