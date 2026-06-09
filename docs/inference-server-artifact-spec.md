# Inference Server Artifact Integration Spec

## Goal

Update the inference server to load and serve learned declipping model artifacts
exported by `declip-model` in the `declip_state_dict_v1` format.

The server must not depend on identity-STFT artifacts or legacy baseline export
paths. The serving model is reconstructed from the private `declip-model`
Python package plus a private, immutable artifact directory containing:

```text
manifest.json
resolved_config.json
weights.pt
checksums.sha256
```

## Required Inputs

The server must be configured with an artifact URI pointing at a directory, not
a single file.

Recommended environment variable:

```bash
DECLIP_ARTIFACT_URI=gs://<bucket>/declip-model-artifacts/declip_state_dict_v1/declip/<model-name>/<model-version>/
```

Current dev Terraform values from `declip-backend-server`:

```text
project_id:           declip-v2-dev
region:               us-central1
python repo:          declip-python
model artifact bucket: anthonymleedom-declip-v2-dev-models
artifact URI prefix:  gs://anthonymleedom-declip-v2-dev-models/declip-model-artifacts/declip_state_dict_v1/
```

Current dev example for the 48 kHz v005 run:

```bash
DECLIP_ARTIFACT_URI=gs://anthonymleedom-declip-v2-dev-models/declip-model-artifacts/declip_state_dict_v1/declip/tgram-deftan2-v005-48khz/20260605T224918Z/
```

Optional but recommended policy environment variables:

```bash
DECLIP_EXPECTED_ARTIFACT_FORMAT=declip_state_dict_v1
DECLIP_EXPECTED_FORMAT_VERSION=1
DECLIP_EXPECTED_MODEL_FAMILY=declip
DECLIP_EXPECTED_MODEL_NAME=tgram-deftan2-v005-48khz
DECLIP_EXPECTED_MODEL_VERSION=20260605T224918Z
DECLIP_EXPECTED_DECLIP_MODEL_VERSION=0.1.1
DECLIP_EXPECTED_TRAINING_GIT_REVISION=<commit-sha>
```

The exact variable names can follow backend conventions, but the server must
validate these values somewhere explicit.

## Private Package Setup

The inference server needs the `declip-model` Python package installed because
`weights.pt` is a PyTorch state dict, not a self-contained model.

Preferred production setup:

1. Keep the `declip-model` GitHub repo private.
2. Build a wheel from `declip-model`.
3. Publish the wheel to the private Artifact Registry Python repository managed
   by Terraform.
4. Pin the backend dependency to the exact package version expected by the
   artifact.

Current dev Artifact Registry Python repository:

```text
https://us-central1-python.pkg.dev/declip-v2-dev/declip-python/
```

The wheel can be published from `declip-model` with:

```bash
scripts/publish_python_wheel.sh
```

That script defaults to:

```text
project:    declip-v2-dev
location:   us-central1
repository: declip-python
```

Backend dependency example:

```toml
dependencies = [
  "declip-model==0.1.1",
]
```

Development-only alternative:

```toml
dependencies = [
  "declip-model @ git+ssh://git@github.com/<org>/<repo>.git@<commit-sha>",
]
```

Avoid requiring GitHub credentials inside production Docker builds if Artifact
Registry is available.

## Artifact Contract

The backend must download or read these files from `DECLIP_ARTIFACT_URI`:

```text
manifest.json
resolved_config.json
weights.pt
checksums.sha256
```

`manifest.json` contains:

```json
{
  "artifact_format": "declip_state_dict_v1",
  "format_version": 1,
  "serving_identity": {
    "model_family": "declip",
    "model_name": "...",
    "model_version": "..."
  },
  "source": {
    "run_dir_name": "...",
    "checkpoint": "best.pt",
    "checkpoint_metadata": {
      "checkpoint": "best.pt",
      "completed_epoch": 1,
      "global_step": 123,
      "validation_loss": 0.123,
      "best_validation_loss": 0.123
    },
    "config_fingerprint": "...",
    "model_schema": "tgram_deftan2_v1",
    "training_code": {
      "git_revision": "...",
      "git_dirty": false,
      "declip_model_version": "0.1.1",
      "python_version": "3.13.12"
    }
  },
  "runtime_contract": {
    "runtime": "pytorch_state_dict",
    "required_package": "declip-model",
    "python_version": "...",
    "torch_version": "...",
    "declip_model_version": "..."
  },
  "audio_model_contract": {
    "sample_rate": 16000,
    "required_channels": 1,
    "dtype": "float32",
    "amplitude_policy": {
      "normalization_policy": "none",
      "amplitude_min": -1.0,
      "amplitude_max": 1.0
    },
    "architecture": "tgramnet_deftan2_waveform",
    "model_config": {}
  },
  "tensor_contract": {
    "input_shape": "[batch, samples]",
    "output_shape": "[batch, samples]",
    "channels": "mono",
    "output_must_match_input_shape": true,
    "output_must_be_finite": true
  },
  "recommended_chunk_samples": 16000,
  "weight_dtype_policy": "preserve"
}
```

`weights.pt` contains only:

```python
{
    "artifact_format": "declip_state_dict_v1",
    "format_version": 1,
    "model_state_dict": ...,
    "checkpoint_metadata": {
        "checkpoint": "best.pt",
        "completed_epoch": int,
        "global_step": int,
        "validation_loss": float,
        "best_validation_loss": float,
    },
}
```

It intentionally excludes optimizer, scheduler, discriminator, and RNG state.

## Startup Load Flow

Implement artifact loading at server startup, not per request.

Required startup steps:

1. Resolve `DECLIP_ARTIFACT_URI`.
2. Download or read the four artifact files into a local read-only cache
   directory.
3. Verify `checksums.sha256` before loading `weights.pt`.
4. Parse `manifest.json`.
5. Validate manifest identity and format:
   - `artifact_format == "declip_state_dict_v1"`
   - `format_version == 1`
   - `runtime_contract.runtime == "pytorch_state_dict"`
   - `runtime_contract.required_package == "declip-model"`
   - configured expected `model_family`, `model_name`, and `model_version`
     match `serving_identity`
6. Validate provenance policy:
   - Compare `source.training_code.declip_model_version` to the installed or
     expected `declip-model` package version.
   - Compare `source.training_code.git_revision` to the expected training
     revision when configured.
   - Decide policy for `source.training_code.git_dirty`. Recommended:
     reject dirty training runs in production, allow only in development.
7. Parse `resolved_config.json` with `declip.config.ExperimentConfig`.
8. Rebuild the model with `declip.model.build_model(config.model)`.
9. Load `weights.pt` with `torch.load(..., map_location=device,
   weights_only=True)`.
10. Validate `weights.pt` format/version matches the manifest.
11. Load `payload["model_state_dict"]` into the model.
12. Set `model.eval()`.
13. Run a startup smoke inference:
    - input tensor shape: `[1, manifest["recommended_chunk_samples"]]`
    - dtype: `torch.float32`
    - device: selected serving device
    - context: `torch.inference_mode()`
    - output shape must exactly match input shape
    - output must be finite
14. Store a singleton loaded model object for request handlers.

Reference loading sketch:

```python
import importlib.metadata
import json
from pathlib import Path

import torch

from declip.config import ExperimentConfig
from declip.model import build_model


def load_declip_artifact(local_dir: Path, device: torch.device) -> torch.nn.Module:
    manifest = json.loads((local_dir / "manifest.json").read_text())
    if manifest["artifact_format"] != "declip_state_dict_v1":
        raise ValueError("Unsupported artifact format")
    if manifest["format_version"] != 1:
        raise ValueError("Unsupported artifact format version")

    installed_version = importlib.metadata.version("declip-model")
    training_version = manifest["source"]["training_code"]["declip_model_version"]
    if training_version != installed_version:
        raise ValueError(
            "Artifact was trained with declip-model "
            f"{training_version}, but server has {installed_version}"
        )

    config_payload = json.loads((local_dir / "resolved_config.json").read_text())
    config = ExperimentConfig.model_validate(config_payload)
    model = build_model(config.model).to(device)

    payload = torch.load(
        local_dir / "weights.pt",
        map_location=device,
        weights_only=True,
    )
    if payload["artifact_format"] != manifest["artifact_format"]:
        raise ValueError("Weights artifact format does not match manifest")
    if payload["format_version"] != manifest["format_version"]:
        raise ValueError("Weights format version does not match manifest")

    model.load_state_dict(payload["model_state_dict"])
    model.eval()

    samples = int(manifest["recommended_chunk_samples"])
    with torch.inference_mode():
        smoke_input = torch.zeros((1, samples), dtype=torch.float32, device=device)
        smoke_output = model(smoke_input)
    if smoke_output.shape != smoke_input.shape:
        raise ValueError("Model smoke output shape does not match input")
    if not bool(torch.isfinite(smoke_output).all()):
        raise ValueError("Model smoke output is non-finite")

    return model
```

Adapt the code to backend conventions, but preserve the validation behavior.

## Checksum Verification

`checksums.sha256` uses standard two-space format:

```text
<sha256>  manifest.json
<sha256>  resolved_config.json
<sha256>  weights.pt
```

The backend must recompute SHA-256 for each listed file and fail startup if any
digest differs, any expected file is missing, or the checksums file references
paths outside the local artifact directory.

Do not load `weights.pt` before checksums pass.

## Request-Time Audio Contract

For v1, the model contract is mono waveform batches:

```text
input:  torch.float32 tensor [batch, samples]
output: torch.float32 tensor [batch, samples]
```

The request pipeline must enforce:

- sample rate equals `manifest.audio_model_contract.sample_rate`
- channel count equals `manifest.audio_model_contract.required_channels` (`1`)
- decoded dtype is float32
- waveform amplitude policy matches:
  - `amplitude_min == -1.0`
  - `amplitude_max == 1.0`
  - `normalization_policy == "none"`
- output shape equals input shape
- output is finite

For now, reject unsupported sample rates and non-mono input rather than silently
resampling or downmixing, unless the backend already has an explicit,
tested preprocessing policy.

## Chunking Policy

Use `manifest["recommended_chunk_samples"]` as the initial chunk size.

For the first implementation, prefer one of these simple policies:

1. If input length is less than or equal to the recommended chunk size, pad to
   chunk size, run inference, then trim back to original length.
2. If input length is greater than the recommended chunk size, either reject
   with a clear error or implement deterministic non-overlapping chunking with
   final-chunk padding and trimming.

Do not introduce overlap-add, crossfades, or streaming state in the first pass
unless the backend already has tests for those behaviors. If chunking is added,
test exact output length preservation.

## Device Policy

CPU must work. GPU may be optional and explicit.

Recommended environment variable:

```bash
DECLIP_DEVICE=cpu
```

Allowed values:

```text
cpu
cuda
```

If `DECLIP_DEVICE=cuda`, fail startup if CUDA is unavailable. Do not silently
fall back to CPU in production unless there is an explicit deployment policy for
that fallback.

## GCS Access

Cloud Run should access the artifact through its service account.

In dev, Terraform already manages the model artifact bucket:

```text
bucket: anthonymleedom-declip-v2-dev-models
prefix: declip-model-artifacts/declip_state_dict_v1/
```

Upload the current v005 48 kHz artifact from `declip-model` with:

```bash
gcloud storage cp \
  artifacts/inference/tgram-deftan2-v005-48khz-20260605T224918Z/manifest.json \
  artifacts/inference/tgram-deftan2-v005-48khz-20260605T224918Z/resolved_config.json \
  artifacts/inference/tgram-deftan2-v005-48khz-20260605T224918Z/weights.pt \
  artifacts/inference/tgram-deftan2-v005-48khz-20260605T224918Z/checksums.sha256 \
  gs://anthonymleedom-declip-v2-dev-models/declip-model-artifacts/declip_state_dict_v1/declip/tgram-deftan2-v005-48khz/20260605T224918Z/
```

Verify the upload with:

```bash
gcloud storage ls \
  gs://anthonymleedom-declip-v2-dev-models/declip-model-artifacts/declip_state_dict_v1/declip/tgram-deftan2-v005-48khz/20260605T224918Z/
```

Required IAM for the Cloud Run runtime service account:

```text
roles/storage.objectViewer
```

Terraform should grant it at the narrowest practical scope. The current dev
module grants the inference Cloud Run service account object-viewer access on:

```text
gs://anthonymleedom-declip-v2-dev-models/
```

The artifact path should be immutable. Do not overwrite an existing
`model-version` directory. Promote production by changing configuration or a
separate release pointer, not by replacing files in-place.

Terraform already defines the dev Python package repository, model artifact
bucket, and inference service-account read access. The deployment configuration
still needs:

- the artifact URI environment variable
- any expected identity/version policy variables
- service-account access to the GCS artifact path in each environment
- access to the private Python package or container image registry in each
  environment

## Tests Required In Backend

Add fast tests with a tiny fixture artifact. The fixture can be generated from
`declip-model` test helpers or built once and stored as a small test fixture if
repo policy allows.

Required tests:

1. Successful artifact load:
   - verifies checksums
   - parses manifest/config
   - rebuilds model
   - loads state dict
   - startup smoke forward succeeds
2. Checksum mismatch is rejected before `torch.load`.
3. Missing artifact file is rejected with path context.
4. Unsupported `artifact_format` is rejected.
5. Unsupported `format_version` is rejected.
6. Mismatched serving identity is rejected.
7. Mismatched `declip-model` version policy is rejected.
8. Dirty training run is rejected in production mode, if that policy is enabled.
9. Bad model output shape is rejected.
10. Non-finite model output is rejected.
11. Request audio with wrong sample rate is rejected.
12. Request audio with more than one channel is rejected.
13. Successful request returns same number of samples as input.

## Operational Logging

On startup, log non-secret artifact identity:

- artifact URI
- artifact format/version
- serving identity
- source run dir name
- checkpoint filename
- checkpoint completed epoch/global step
- training git revision
- training `declip-model` version
- installed `declip-model` version
- sample rate/channels
- recommended chunk samples
- selected device

Do not log signed URLs, credentials, raw audio, or full request payloads.

## Failure Behavior

All artifact validation failures should fail server startup. Do not start a
server that can accept traffic without a validated model.

Request-specific media validation failures should return a clear client error.
Model execution failures should return a server error and include enough
internal logging to identify shape, dtype, device, and artifact identity.

## Done Criteria

The backend implementation is complete when:

- It installs private `declip-model` as a pinned dependency.
- It loads `declip_state_dict_v1` artifacts from local disk or GCS.
- It verifies checksums before loading weights.
- It validates manifest format, serving identity, training code provenance, and
  audio/tensor contract.
- It reconstructs the model from `resolved_config.json`.
- It loads `weights.pt` state dict only.
- It performs startup smoke inference.
- It serves mono float32 inference with same-shape finite output.
- It has tests for success and the failure paths listed above.
- Deployment config can point staging/prod at an immutable artifact URI.
