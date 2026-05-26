from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.api import PublicModelCatalog, PublicModelFamily
from app.models.domain import ModelSpec


class ConcreteModelConfig(BaseModel):
    sample_rate_hz: int = Field(gt=0)
    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    artifact_uri: str = Field(min_length=1)
    runtime: str = "pytorch"
    enabled: bool = True

    @field_validator("runtime")
    @classmethod
    def validate_runtime(cls, value: str) -> str:
        if value != "pytorch":
            raise ValueError("runtime must be pytorch")
        return value


class ModelFamilyConfig(BaseModel):
    family: str = Field(min_length=1)
    enabled: bool = True
    display_name: str = Field(min_length=1)
    description: str = ""
    models: list[ConcreteModelConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_family(self) -> "ModelFamilyConfig":
        sample_rates = [model.sample_rate_hz for model in self.models]
        if len(sample_rates) != len(set(sample_rates)):
            raise ValueError(f"duplicate sample rates in family {self.family}")
        if self.enabled and not any(model.enabled for model in self.models):
            raise ValueError(f"enabled family {self.family} requires an enabled model")
        return self


class ModelCatalogConfig(BaseModel):
    catalog_version: str = Field(min_length=1)
    model_families: list[ModelFamilyConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> "ModelCatalogConfig":
        families = [family.family for family in self.model_families]
        if len(families) != len(set(families)):
            raise ValueError("family identifiers must be unique")
        return self


class ModelCatalog:
    def __init__(self, config: ModelCatalogConfig) -> None:
        self._config = config

    @classmethod
    def from_path(cls, path: Path) -> "ModelCatalog":
        with path.open("r", encoding="utf-8") as file:
            raw_config = yaml.safe_load(file) or {}
        return cls(ModelCatalogConfig.model_validate(raw_config))

    def public_catalog(self) -> PublicModelCatalog:
        families: list[PublicModelFamily] = []
        for family in self._config.model_families:
            if not family.enabled:
                continue
            supported_sample_rates = sorted(
                model.sample_rate_hz for model in family.models if model.enabled
            )
            families.append(
                PublicModelFamily(
                    family=family.family,
                    display_name=family.display_name,
                    description=family.description,
                    enabled=True,
                    supported_sample_rates_hz=supported_sample_rates,
                )
            )
        return PublicModelCatalog(
            catalog_version=self._config.catalog_version,
            model_families=families,
        )

    def resolve_model(self, family_id: str, sample_rate_hz: int) -> ModelSpec:
        for family in self._config.model_families:
            if family.family != family_id or not family.enabled:
                continue
            for model in family.models:
                if model.sample_rate_hz == sample_rate_hz and model.enabled:
                    return ModelSpec(
                        family=family.family,
                        model_name=model.model_name,
                        model_version=model.model_version,
                        sample_rate_hz=model.sample_rate_hz,
                        artifact_uri=model.artifact_uri,
                        runtime=model.runtime,
                    )
        raise LookupError(f"No enabled model for family {family_id} at {sample_rate_hz} Hz")
