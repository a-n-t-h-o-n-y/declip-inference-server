import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.errors import PermanentInferenceError


@dataclass(frozen=True)
class AudioMetadata:
    duration_seconds: float
    sample_rate_hz: int
    channels: int
    format_name: str | None = None
    bit_depth: int | None = None


class AudioProbeService(Protocol):
    def probe(self, path: Path) -> AudioMetadata:
        """Decode/probe audio metadata for a local file."""


class FfprobeAudioProbeService:
    def probe(self, path: Path) -> AudioMetadata:
        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=codec_type,sample_rate,channels,bits_per_sample",
            "-of",
            "json",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            raise PermanentInferenceError("invalid_audio", "Input audio could not be decoded.") from exc

        try:
            payload = json.loads(completed.stdout)
            audio_stream = next(
                stream
                for stream in payload.get("streams", [])
                if stream.get("codec_type") == "audio"
            )
            duration = float(payload.get("format", {}).get("duration"))
            sample_rate = int(audio_stream["sample_rate"])
            channels = int(audio_stream["channels"])
            raw_bit_depth = audio_stream.get("bits_per_sample")
            bit_depth = int(raw_bit_depth) if raw_bit_depth not in {None, 0, "0"} else None
        except (StopIteration, KeyError, TypeError, ValueError) as exc:
            raise PermanentInferenceError("invalid_audio", "Input audio metadata is invalid.") from exc

        return AudioMetadata(
            duration_seconds=duration,
            sample_rate_hz=sample_rate,
            channels=channels,
            format_name=payload.get("format", {}).get("format_name"),
            bit_depth=bit_depth,
        )


class StaticAudioProbeService:
    def __init__(self, metadata: AudioMetadata) -> None:
        self.metadata = metadata

    def probe(self, path: Path) -> AudioMetadata:
        return self.metadata


def validate_audio_metadata(
    metadata: AudioMetadata,
    expected_sample_rate_hz: int,
    expected_channels: int,
    max_duration_seconds: int,
) -> None:
    if metadata.duration_seconds <= 0:
        raise PermanentInferenceError("invalid_audio", "Input audio duration is invalid.")
    if metadata.duration_seconds > max_duration_seconds:
        raise PermanentInferenceError("audio_too_long", "Input audio exceeds the maximum duration.")
    if metadata.sample_rate_hz != expected_sample_rate_hz:
        raise PermanentInferenceError(
            "sample_rate_mismatch",
            "Decoded audio sample rate does not match the queued job.",
        )
    if metadata.channels != expected_channels:
        raise PermanentInferenceError(
            "channel_count_mismatch",
            "Decoded audio channel count does not match the queued job.",
        )
