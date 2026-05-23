from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_app_settings
from app.core.config import Settings
from app.models.api import HealthResponse, VersionResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(settings: Annotated[Settings, Depends(get_app_settings)]) -> HealthResponse:
    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        environment=settings.app_env,
        version=settings.app_version,
    )


@router.get("/version", response_model=VersionResponse)
async def version(settings: Annotated[Settings, Depends(get_app_settings)]) -> VersionResponse:
    return VersionResponse(
        app_name=settings.app_name,
        environment=settings.app_env,
        version=settings.app_version,
    )
