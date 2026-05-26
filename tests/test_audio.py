import pytest

from app.services.audio import AudioMetadata, validate_audio_metadata
from app.core.errors import PermanentInferenceError


def test_audio_validation_accepts_matching_metadata() -> None:
    validate_audio_metadata(
        AudioMetadata(duration_seconds=10.0, sample_rate_hz=44100, channels=2),
        expected_sample_rate_hz=44100,
        expected_channels=2,
        max_duration_seconds=1200,
    )


@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        (AudioMetadata(duration_seconds=0, sample_rate_hz=44100, channels=2), "invalid_audio"),
        (AudioMetadata(duration_seconds=1201, sample_rate_hz=44100, channels=2), "audio_too_long"),
        (
            AudioMetadata(duration_seconds=10, sample_rate_hz=48000, channels=2),
            "sample_rate_mismatch",
        ),
        (
            AudioMetadata(duration_seconds=10, sample_rate_hz=44100, channels=1),
            "channel_count_mismatch",
        ),
    ],
)
def test_audio_validation_raises_user_safe_permanent_errors(
    metadata: AudioMetadata, code: str
) -> None:
    with pytest.raises(PermanentInferenceError) as exc_info:
        validate_audio_metadata(
            metadata,
            expected_sample_rate_hz=44100,
            expected_channels=2,
            max_duration_seconds=1200,
        )

    assert exc_info.value.code == code


def test_canonical_pcm_validation_requires_float_wav_codec() -> None:
    from app.services.audio import validate_canonical_pcm_metadata

    validate_canonical_pcm_metadata(
        AudioMetadata(
            duration_seconds=10.0,
            sample_rate_hz=44100,
            channels=2,
            format_name="wav",
            codec_name="pcm_f32le",
        ),
        expected_sample_rate_hz=44100,
        expected_channels=2,
        max_duration_seconds=1200,
    )

    with pytest.raises(PermanentInferenceError) as exc_info:
        validate_canonical_pcm_metadata(
            AudioMetadata(
                duration_seconds=10.0,
                sample_rate_hz=44100,
                channels=2,
                format_name="wav",
                codec_name="pcm_s16le",
            ),
            expected_sample_rate_hz=44100,
            expected_channels=2,
            max_duration_seconds=1200,
        )

    assert exc_info.value.code == "invalid_processing_audio"
