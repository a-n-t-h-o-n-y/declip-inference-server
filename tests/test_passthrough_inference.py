from app.models.domain import JobRecord, ModelSpec
from app.services.audio import AudioMetadata, StaticAudioProbeService
from app.services.inference import PassthroughInferenceRunner
from app.services.storage import InMemoryStorageService


def test_passthrough_inference_copies_canonical_model_input_to_model_output() -> None:
    storage = InMemoryStorageService({"gs://bucket/working/model-input.f32.wav": b"audio-bytes"})
    runner = PassthroughInferenceRunner(
        storage=storage,
        audio_probe=StaticAudioProbeService(
            AudioMetadata(
                duration_seconds=2.5,
                sample_rate_hz=44100,
                channels=1,
                format_name="wav",
                codec_name="pcm_f32le",
            )
        ),
        max_duration_seconds=1200,
    )

    result = runner.run(
        job=JobRecord(
            id="job_1",
            user_id="user_1",
            status="processing",
            model_family="ddd-v1",
            input_gcs_uri=None,
            input_duration_seconds=2.5,
            input_sample_rate_hz=44100,
            input_channels=1,
            model_input_gcs_uri="gs://bucket/working/model-input.f32.wav",
            model_output_gcs_uri="gs://bucket/working/model-output.f32.wav",
        ),
        model=ModelSpec(
            family="ddd-v1",
            model_name="ddd-v1-44k",
            model_version="1.0.0",
            sample_rate_hz=44100,
            artifact_uri="gs://models/model.pt",
        ),
    )

    assert storage.objects["gs://bucket/working/model-output.f32.wav"] == b"audio-bytes"
    assert storage.content_types["gs://bucket/working/model-output.f32.wav"] == "audio/wav"
    assert (
        storage.metadata["gs://bucket/working/model-output.f32.wav"]["inference_backend"]
        == "passthrough"
    )
    assert result.output_size_bytes == len(b"audio-bytes")
    assert result.model_output_gcs_uri == "gs://bucket/working/model-output.f32.wav"


def test_passthrough_inference_requires_working_object_uris() -> None:
    import pytest

    from app.core.errors import PermanentInferenceError

    storage = InMemoryStorageService()
    runner = PassthroughInferenceRunner(
        storage=storage,
        audio_probe=StaticAudioProbeService(
            AudioMetadata(
                duration_seconds=2.5,
                sample_rate_hz=44100,
                channels=1,
                format_name="wav",
                codec_name="pcm_f32le",
            )
        ),
        max_duration_seconds=1200,
    )

    with pytest.raises(PermanentInferenceError) as exc_info:
        runner.run(
            job=JobRecord(
                id="job_1",
                user_id="user_1",
                status="processing",
                model_family="ddd-v1",
                input_gcs_uri=None,
                input_duration_seconds=2.5,
                input_sample_rate_hz=44100,
                input_channels=1,
            ),
            model=ModelSpec(
                family="ddd-v1",
                model_name="ddd-v1-44k",
                model_version="1.0.0",
                sample_rate_hz=44100,
                artifact_uri="gs://models/model.pt",
            ),
        )

    assert exc_info.value.code == "invalid_job_audio"
