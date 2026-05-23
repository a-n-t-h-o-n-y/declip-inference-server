from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings
from app.models.domain import VerifiedServiceToken
from app.services.model_catalog import ModelCatalog
from app.services.task_auth import ServiceTokenVerifier, TokenVerificationError
from app.services.task_processing import TaskProcessor


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_model_catalog(request: Request) -> ModelCatalog:
    return request.app.state.model_catalog


def get_token_verifier(request: Request) -> ServiceTokenVerifier:
    return request.app.state.token_verifier


def get_task_processor(request: Request) -> TaskProcessor:
    return request.app.state.task_processor


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "missing_service_token", "message": "Unauthorized."},
        )
    return token


async def require_internal_service_token(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    verifier: Annotated[ServiceTokenVerifier, Depends(get_token_verifier)],
) -> VerifiedServiceToken:
    token = _bearer_token(request)
    try:
        verified = verifier.verify(token)
    except TokenVerificationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_service_token", "message": "Unauthorized."},
        ) from None

    if verified.email not in settings.allowed_internal_caller_service_accounts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden_service_account", "message": "Forbidden."},
        )
    return verified


async def require_task_service_token(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
    verifier: Annotated[ServiceTokenVerifier, Depends(get_token_verifier)],
) -> VerifiedServiceToken:
    token = _bearer_token(request)
    try:
        verified = verifier.verify(token)
    except TokenVerificationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_task_token", "message": "Unauthorized."},
        ) from None

    if not settings.cloud_tasks_service_account:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "task_auth_not_configured",
                "message": "Task authentication is not configured.",
            },
        )
    if verified.email != settings.cloud_tasks_service_account:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden_service_account", "message": "Forbidden."},
        )
    return verified
