from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.api.routes.internal import router as internal_router
from app.api.routes.tasks import router as tasks_router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.services.database import InMemoryJobRepository
from app.services.model_catalog import ModelCatalog
from app.services.quotas import InMemoryQuotaService
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
    app.state.job_repository = InMemoryJobRepository()
    app.state.quota_service = InMemoryQuotaService()
    app.state.task_processor = TaskProcessor(
        jobs=app.state.job_repository,
        quotas=app.state.quota_service,
        catalog=catalog,
        inference=FakeInferenceRunner(),
    )

    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    app.include_router(health_router)
    app.include_router(internal_router)
    app.include_router(tasks_router)
    return app


app = create_app()
