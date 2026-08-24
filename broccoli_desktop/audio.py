"""Continuous native-rate capture conversion and Listening frame batching."""

from __future__ import annotations

from typing import Literal, Protocol

import numpy
import soxr

from broccoli_desktop.models import AudioFrame

OUTPUT_SAMPLE_RATE = 24_000
INPUT_BLOCK_MS = 20
OUTPUT_FRAME_MS = 100
OUTPUT_FRAME_BYTES = OUTPUT_SAMPLE_RATE * OUTPUT_FRAME_MS // 1_000 * 2

#: The rates a 20 ms block may plausibly arrive at. Windows mix formats run
#: 8 kHz (Bluetooth HFP) through 192 kHz; anything outside is a length bug,
#: not a device.
MIN_INPUT_SAMPLE_RATE = 8_000
MAX_INPUT_SAMPLE_RATE = 192_000


class ResampleStream(Protocol):
    """One continuous mono PCM16 conversion fed block by block."""

    def process(self, pcm: bytes) -> bytes: ...


class ResamplerFactory(Protocol):
    def create(self, input_rate: int, output_rate: int) -> ResampleStream: ...


class _SoxrResampleStream:
    def __init__(self, input_rate: int, output_rate: int) -> None:
        self._stream = soxr.ResampleStream(input_rate, output_rate, 1, dtype="int16", quality="HQ")

    def process(self, pcm: bytes) -> bytes:
        samples = numpy.frombuffer(pcm, dtype="<i2")
        return self._stream.resample_chunk(samples).astype("<i2", copy=False).tobytes()


class SoxrResamplerFactory:
    """Build stateful soxr conversions without retaining capture data.

    A stream per channel, not a call per block: one-shot ``soxr.resample``
    treats every 20 ms block as a complete signal, restarting its filter at
    each boundary. Those edge transients repeated 50 times a second measured
    ~31 dB SNR against the continuous conversion on speech-band material --
    structured buzz over everything the transcription model hears -- while
    the stateful stream measures ~70 dB. The held filter tail also means the
    output lags the input by a few milliseconds, which the 100 ms framing
    absorbs without moving any offset.
    """

    def create(self, input_rate: int, output_rate: int) -> _SoxrResampleStream:
        return _SoxrResampleStream(input_rate, output_rate)


class AudioPipeline:
    """Build per-channel remote frames from every fixed 20 ms capture block.

    Blocks arrive as 20 ms of mono PCM16 at whatever rate the capture device
    natively runs, so the block's length names its rate; each channel keeps
    one resampler stream for as long as that rate holds.
    """

    def __init__(
        self,
        *,
        resampler_factory: ResamplerFactory | None = None,
        base_offset_ms: int = 0,
    ) -> None:
        if base_offset_ms < 0:
            raise ValueError("base_offset_ms must be non-negative")
        self._resampler_factory = resampler_factory or SoxrResamplerFactory()
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
        self._streams: dict[Literal["mic", "system"], tuple[int, ResampleStream] | None] = {
            "mic": None,
            "system": None,
        }

    def feed(self, channel: Literal["mic", "system"], pcm: bytes) -> list[AudioFrame]:
        """Accept exactly one 20 ms mono PCM16 block and return completed frames."""
        if channel not in self._buffers:
            raise ValueError("Unknown audio channel.")
        input_rate = (len(pcm) // 2) * (1_000 // INPUT_BLOCK_MS)
        if (
            not pcm
            or len(pcm) % 2
            or not MIN_INPUT_SAMPLE_RATE <= input_rate <= MAX_INPUT_SAMPLE_RATE
        ):
            raise ValueError("PCM input must be exactly one 20 ms mono PCM16 block.")

        capture_offset_ms = self._capture_offsets_ms[channel]
        self._capture_offsets_ms[channel] += INPUT_BLOCK_MS

        buffer = self._buffers[channel]
        # Stamped from the block that feeds the stream, not the first block
        # whose output arrives: the stream's held filter delay can swallow an
        # entire first block, and the samples it eventually releases belong to
        # the block that went in, at the offset the meeting had then.
        if not buffer and self._buffer_offsets_ms[channel] is None:
            self._buffer_offsets_ms[channel] = capture_offset_ms

        stream_entry = self._streams[channel]
        if stream_entry is None or stream_entry[0] != input_rate:
            stream_entry = (
                input_rate,
                self._resampler_factory.create(input_rate, OUTPUT_SAMPLE_RATE),
            )
            self._streams[channel] = stream_entry
        pcm_24k = stream_entry[1].process(pcm)
        if len(pcm_24k) % 2:
            raise ValueError("Resampler returned non-PCM16 data.")
        if not pcm_24k:
            return []
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

    def skip(self, channel: Literal["mic", "system"]) -> None:
        """Count one 20 ms block for a muted channel without producing a frame.

        The offset is a position in the meeting, not a count of what was sent.
        A muted channel whose clock stopped would come back, on unmute, at
        offsets the meeting already used, and its new speech would land on top
        of transcript that is already there.

        The partial buffer goes with it: the tail of a frame captured before
        the mute must not be completed with audio from after it, and it must
        not be emitted late under an offset the mute has already moved past.
        The resampler stream goes too -- its filter still holds the last
        milliseconds from before the mute, and they must not color the first
        audio after it.
        """
        if channel not in self._buffers:
            raise ValueError("Unknown audio channel.")
        self._capture_offsets_ms[channel] += INPUT_BLOCK_MS
        self._buffers[channel].clear()
        self._buffer_offsets_ms[channel] = None
        self._streams[channel] = None

    @property
    def next_offset_ms(self) -> int:
        """Return the next shared offset after all captured channel input."""
        return self._base_offset_ms + max(self._capture_offsets_ms.values())
