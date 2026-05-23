from app.models.domain import JobRecord, ModelSpec
from app.services.audio import AudioMetadata, StaticAudioProbeService
from app.services.inference import PassthroughInferenceRunner, output_uri_from_input
from app.services.storage import InMemoryStorageService


def test_passthrough_inference_copies_input_to_output() -> None:
    storage = InMemoryStorageService({"gs://bucket/input.wav": b"audio-bytes"})
    runner = PassthroughInferenceRunner(
        storage=storage,
        audio_probe=StaticAudioProbeService(
            AudioMetadata(duration_seconds=2.5, sample_rate_hz=44100, channels=1)
        ),
        max_duration_seconds=1200,
    )

    result = runner.run(
        job=JobRecord(
            id="job_1",
            user_id="user_1",
            status="processing",
            model_family="ddd-v1",
            input_gcs_uri="gs://bucket/input.wav",
            input_duration_seconds=2.5,
            input_sample_rate_hz=44100,
            input_channels=1,
            input_content_type="audio/wav",
            output_gcs_uri="gs://bucket/output.wav",
        ),
        model=ModelSpec(
            family="ddd-v1",
            model_name="ddd-v1-44k",
            model_version="1.0.0",
            sample_rate_hz=44100,
            artifact_uri="gs://models/model.pt",
        ),
    )

    assert storage.objects["gs://bucket/output.wav"] == b"audio-bytes"
    assert storage.content_types["gs://bucket/output.wav"] == "audio/wav"
    assert storage.metadata["gs://bucket/output.wav"]["inference_backend"] == "passthrough"
    assert result.output_size_bytes == len(b"audio-bytes")
    assert result.output_format == "wav"


def test_passthrough_inference_derives_output_uri_when_missing() -> None:
    storage = InMemoryStorageService(
        {"gs://bucket/users/user_1/jobs/job_1/input/clip.wav": b"audio-bytes"}
    )
    runner = PassthroughInferenceRunner(
        storage=storage,
        audio_probe=StaticAudioProbeService(
            AudioMetadata(duration_seconds=2.5, sample_rate_hz=44100, channels=1)
        ),
        max_duration_seconds=1200,
    )

    result = runner.run(
        job=JobRecord(
            id="job_1",
            user_id="user_1",
            status="processing",
            model_family="ddd-v1",
            input_gcs_uri="gs://bucket/users/user_1/jobs/job_1/input/clip.wav",
            input_duration_seconds=2.5,
            input_sample_rate_hz=44100,
            input_channels=1,
            input_content_type="audio/wav",
        ),
        model=ModelSpec(
            family="ddd-v1",
            model_name="ddd-v1-44k",
            model_version="1.0.0",
            sample_rate_hz=44100,
            artifact_uri="gs://models/model.pt",
        ),
    )

    assert result.output_gcs_uri == "gs://bucket/users/user_1/jobs/job_1/output/clip.wav"
    assert storage.objects[result.output_gcs_uri] == b"audio-bytes"


def test_output_uri_from_input_uses_job_output_layout() -> None:
    output_uri = output_uri_from_input(
        "gs://bucket/users/user_1/jobs/job_1/input/nested/clip.wav",
        "job_1",
    )

    assert output_uri == "gs://bucket/users/user_1/jobs/job_1/output/clip.wav"
