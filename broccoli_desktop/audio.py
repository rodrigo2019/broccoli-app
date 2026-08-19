"""VAD-gated 48 kHz capture conversion and Listening frame batching."""

from __future__ import annotations

from array import array
from typing import Literal, Protocol

import soxr
import webrtcvad

from broccoli_desktop.models import AudioFrame

INPUT_SAMPLE_RATE = 48_000
OUTPUT_SAMPLE_RATE = 24_000
INPUT_BLOCK_MS = 20
INPUT_BLOCK_BYTES = INPUT_SAMPLE_RATE * INPUT_BLOCK_MS // 1_000 * 2
OUTPUT_FRAME_MS = 100
OUTPUT_FRAME_BYTES = OUTPUT_SAMPLE_RATE * OUTPUT_FRAME_MS // 1_000 * 2


class Vad(Protocol):
    def is_speech(self, pcm: bytes, sample_rate: int) -> bool: ...


class Resampler(Protocol):
    def resample(self, pcm: bytes, input_rate: int, output_rate: int) -> bytes: ...


class SoxrResampler:
    """Convert mono signed PCM16 samples without retaining capture data."""

    def resample(self, pcm: bytes, input_rate: int, output_rate: int) -> bytes:
        samples = array("h")
        samples.frombytes(pcm)
        converted = soxr.resample(samples, input_rate, output_rate, quality="HQ")
        return converted.astype("<i2", copy=False).tobytes()


class AudioPipeline:
    """Build per-channel remote frames from fixed 20 ms capture blocks."""

    def __init__(
        self,
        *,
        vad: Vad | None = None,
        resampler: Resampler | None = None,
        base_offset_ms: int = 0,
    ) -> None:
        if base_offset_ms < 0:
            raise ValueError("base_offset_ms must be non-negative")
        self._vad = vad or webrtcvad.Vad(2)
        self._resampler = resampler or SoxrResampler()
        self._base_offset_ms = base_offset_ms
        self._capture_offsets_ms: dict[Literal["mic", "system"], int] = {
            "mic": 0,
            "system": 0,
        }
        self._buffers = {"mic": bytearray(), "system": bytearray()}
        self._buffer_offsets_ms: dict[Literal["mic", "system"], int | None] = {
            "mic": None,
            "system": None,
        }

    def feed(self, channel: Literal["mic", "system"], pcm_48k: bytes) -> list[AudioFrame]:
        """Accept exactly one 20 ms mono PCM16 block and return completed frames."""
        if channel not in self._buffers:
            raise ValueError("Unknown audio channel.")
        if len(pcm_48k) != INPUT_BLOCK_BYTES:
            raise ValueError("PCM input must be exactly one 20 ms 48 kHz mono PCM16 block.")

        capture_offset_ms = self._capture_offsets_ms[channel]
        self._capture_offsets_ms[channel] += INPUT_BLOCK_MS
        if not self._vad.is_speech(pcm_48k, INPUT_SAMPLE_RATE):
            return []

        pcm_24k = self._resampler.resample(pcm_48k, INPUT_SAMPLE_RATE, OUTPUT_SAMPLE_RATE)
        if len(pcm_24k) % 2:
            raise ValueError("Resampler returned non-PCM16 data.")
        if not pcm_24k:
            return []

        buffer = self._buffers[channel]
        if not buffer:
            self._buffer_offsets_ms[channel] = capture_offset_ms
        buffer.extend(pcm_24k)
        frames: list[AudioFrame] = []
        while len(buffer) >= OUTPUT_FRAME_BYTES:
            start_offset_ms = self._buffer_offsets_ms[channel]
            if start_offset_ms is None:
                raise RuntimeError("Audio frame buffer offset was not initialized.")
            frames.append(
                AudioFrame(
                    channel=channel,
                    offset_ms=self._base_offset_ms + start_offset_ms,
                    pcm=bytes(buffer[:OUTPUT_FRAME_BYTES]),
                )
            )
            del buffer[:OUTPUT_FRAME_BYTES]
            self._buffer_offsets_ms[channel] = start_offset_ms + OUTPUT_FRAME_MS
        if not buffer:
            self._buffer_offsets_ms[channel] = None
        return frames
