from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.model_catalog import ModelCatalog, ModelCatalogConfig


def test_public_catalog_omits_private_artifact_uris() -> None:
    catalog = ModelCatalog.from_path(Path("config/models.yaml"))

    response = catalog.public_catalog().model_dump()

    assert response["catalog_version"] == "0.1.0"
    assert response["model_families"] == [
        {
            "family": "ddd-v1",
            "display_name": "DDD v1",
            "description": "General declipping model family.",
            "enabled": True,
            "supported_sample_rates_hz": [44100, 48000],
            "default_output_format": "wav",
        }
    ]
    assert "artifact_uri" not in str(response)


def test_duplicate_families_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelCatalogConfig.model_validate(
            {
                "catalog_version": "0.1.0",
                "model_families": [
                    _family("ddd-v1", 44100),
                    _family("ddd-v1", 48000),
                ],
            }
        )


def test_duplicate_sample_rates_are_rejected() -> None:
    family = _family("ddd-v1", 44100)
    family["models"].append(family["models"][0] | {"model_name": "duplicate"})

    with pytest.raises(ValidationError):
        ModelCatalogConfig.model_validate(
            {"catalog_version": "0.1.0", "model_families": [family]}
        )


def _family(family_id: str, sample_rate: int) -> dict:
    return {
        "family": family_id,
        "enabled": True,
        "display_name": family_id,
        "description": "",
        "models": [
            {
                "sample_rate_hz": sample_rate,
                "model_name": f"{family_id}-{sample_rate}",
                "model_version": "1.0.0",
                "artifact_uri": "gs://bucket/model.pt",
                "runtime": "pytorch",
                "enabled": True,
            }
        ],
    }
