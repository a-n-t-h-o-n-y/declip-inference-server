from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    app_name: str
    environment: str
    version: str


class VersionResponse(BaseModel):
    app_name: str
    environment: str
    version: str


class PublicModelFamily(BaseModel):
    family: str
    display_name: str
    description: str
    enabled: bool
    supported_sample_rates_hz: list[int]


class PublicModelCatalog(BaseModel):
    catalog_version: str
    model_families: list[PublicModelFamily]


class ProcessJobRequest(BaseModel):
    job_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    request_id: str | None = None
    trace_id: str | None = None


class ProcessJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str
    processing_stage: str | None = None
    attempt: int
