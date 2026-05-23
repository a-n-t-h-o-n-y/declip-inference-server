from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_model_catalog, require_internal_service_token
from app.models.api import PublicModelCatalog
from app.models.domain import VerifiedServiceToken
from app.services.model_catalog import ModelCatalog

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/model-catalog", response_model=PublicModelCatalog)
async def get_internal_model_catalog(
    catalog: Annotated[ModelCatalog, Depends(get_model_catalog)],
    _token: Annotated[VerifiedServiceToken, Depends(require_internal_service_token)],
) -> PublicModelCatalog:
    return catalog.public_catalog()
