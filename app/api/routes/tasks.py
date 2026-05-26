from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_task_processor, require_task_service_token
from app.models.api import ProcessJobRequest, ProcessJobResponse
from app.models.domain import VerifiedServiceToken
from app.services.task_processing import TaskProcessor

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/process-job", response_model=ProcessJobResponse)
async def process_job(
    payload: ProcessJobRequest,
    _token: Annotated[VerifiedServiceToken, Depends(require_task_service_token)],
    processor: Annotated[TaskProcessor, Depends(get_task_processor)],
) -> ProcessJobResponse:
    result = processor.process(payload)
    return ProcessJobResponse(
        job_id=result.job_id,
        status=result.status,
        processing_stage=result.processing_stage,
        attempt=payload.attempt,
    )
