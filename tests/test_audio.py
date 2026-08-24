from __future__ import annotations

from dataclasses import dataclass, field

import numpy
import pytest
import soxr

from broccoli_desktop.audio import AudioPipeline, SoxrResamplerFactory


def pcm_20ms(rate: int = 48_000) -> bytes:
    return b"\x01\x00" * (rate * 20 // 1_000)


def silent_pcm_20ms() -> bytes:
    return bytes(1_920)


@dataclass
class FakeResampleStream:
    output_bytes: int
    processed: list[bytes] = field(default_factory=list)

    def process(self, pcm: bytes) -> bytes:
        self.processed.append(pcm)
        return b"\x02\x00" * (self.output_bytes // 2)


@dataclass
class FakeResamplerFactory:
    output_bytes: int = 960
    created: list[tuple[int, int]] = field(default_factory=list)
    streams: list[FakeResampleStream] = field(default_factory=list)

    def create(self, input_rate: int, output_rate: int) -> FakeResampleStream:
        self.created.append((input_rate, output_rate))
        stream = FakeResampleStream(self.output_bytes)
        self.streams.append(stream)
        return stream


@pytest.fixture
def fake_factory() -> FakeResamplerFactory:
    return FakeResamplerFactory()


def test_pipeline_emits_one_100ms_24khz_frame_after_five_capture_blocks(
    fake_factory: FakeResamplerFactory,
) -> None:
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    frames = [frame for _ in range(5) for frame in pipeline.feed("system", pcm_20ms())]

    assert len(frames) == 1
    assert frames[0].channel == "system"
    assert len(frames[0].pcm) == 4_800
    assert frames[0].offset_ms == 0


def test_pipeline_forwards_silence_to_the_remote_vad_without_creating_a_gap(
    fake_factory: FakeResamplerFactory,
) -> None:
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    frames = [frame for _ in range(5) for frame in pipeline.feed("mic", silent_pcm_20ms())]

    assert len(frames) == 1
    assert frames[0].offset_ms == 0


def test_pipeline_keeps_mic_and_system_accumulation_independent(
    fake_factory: FakeResamplerFactory,
) -> None:
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    mic_before = [frame for _ in range(4) for frame in pipeline.feed("mic", pcm_20ms())]
    system_frames = [frame for _ in range(5) for frame in pipeline.feed("system", pcm_20ms())]
    mic_after = pipeline.feed("mic", pcm_20ms())

    assert mic_before == []
    assert [frame.channel for frame in system_frames] == ["system"]
    assert [frame.channel for frame in mic_after] == ["mic"]


def test_interleaved_sources_share_the_same_capture_period_offsets(
    fake_factory: FakeResamplerFactory,
) -> None:
    pipeline = AudioPipeline(resampler_factory=fake_factory, base_offset_ms=12_345)

    frames = [
        frame
        for _ in range(5)
        for channel in ("mic", "system")
        for frame in pipeline.feed(channel, pcm_20ms())
    ]

    assert {frame.channel: frame.offset_ms for frame in frames} == {
        "mic": 12_345,
        "system": 12_345,
    }


def test_pipeline_preserves_resampled_carry_over_between_frames() -> None:
    pipeline = AudioPipeline(resampler_factory=FakeResamplerFactory(output_bytes=1_000))

    first = [frame for _ in range(5) for frame in pipeline.feed("mic", pcm_20ms())]
    before_next_frame = [frame for _ in range(4) for frame in pipeline.feed("mic", pcm_20ms())]
    second = pipeline.feed("mic", pcm_20ms())

    assert len(first) == 1
    assert len(first[0].pcm) == 4_800
    assert before_next_frame == []
    assert len(second) == 1
    assert len(second[0].pcm) == 4_800
    assert second[0].offset_ms == 100


def test_resumed_pipeline_offsets_frames_after_the_remote_base(
    fake_factory: FakeResamplerFactory,
) -> None:
    pipeline = AudioPipeline(resampler_factory=fake_factory, base_offset_ms=12_345)

    frames = [frame for _ in range(5) for frame in pipeline.feed("mic", pcm_20ms())]

    assert len(frames) == 1
    assert frames[0].offset_ms == 12_345


def test_a_skipped_block_produces_no_frame_but_still_moves_the_channel_clock(
    fake_factory: FakeResamplerFactory,
) -> None:
    """Muting withholds the audio; it does not rewind the meeting."""
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    for _ in range(5):
        pipeline.skip("mic")
    frames = [frame for _ in range(5) for frame in pipeline.feed("mic", pcm_20ms())]

    assert len(frames) == 1
    assert frames[0].offset_ms == 100


def test_skipping_drops_the_partial_frame_captured_before_the_mute(
    fake_factory: FakeResamplerFactory,
) -> None:
    """Audio from before a mute must not be completed with audio from after it."""
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    for _ in range(3):
        pipeline.feed("mic", pcm_20ms())
    pipeline.skip("mic")
    frames = [frame for _ in range(4) for frame in pipeline.feed("mic", pcm_20ms())]

    assert frames == []
    assert [frame.offset_ms for frame in pipeline.feed("mic", pcm_20ms())] == [80]


def test_skipping_one_channel_leaves_the_other_untouched(
    fake_factory: FakeResamplerFactory,
) -> None:
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    for _ in range(5):
        pipeline.skip("mic")
    frames = [frame for _ in range(5) for frame in pipeline.feed("system", pcm_20ms())]

    assert [frame.offset_ms for frame in frames] == [0]
    assert pipeline.next_offset_ms == 100


def test_each_channel_streams_through_its_own_persistent_resampler(
    fake_factory: FakeResamplerFactory,
) -> None:
    """Consecutive blocks must reach one stateful stream, not fresh instances.

    Per-block one-shot conversion restarts the resampling filter at every
    block boundary, and the resulting edge artifacts repeat 50 times per
    second across everything the transcription model hears.
    """
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    for _ in range(3):
        pipeline.feed("mic", pcm_20ms())
    pipeline.feed("system", pcm_20ms())

    assert fake_factory.created == [(48_000, 24_000), (48_000, 24_000)]
    assert [len(stream.processed) for stream in fake_factory.streams] == [3, 1]


def test_pipeline_accepts_blocks_at_any_native_device_rate(
    fake_factory: FakeResamplerFactory,
) -> None:
    """A 20 ms mono PCM16 block names its own rate through its length."""
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    pipeline.feed("mic", pcm_20ms(rate=44_100))
    pipeline.feed("system", pcm_20ms(rate=16_000))

    assert fake_factory.created == [(44_100, 24_000), (16_000, 24_000)]


def test_a_changed_block_rate_starts_a_fresh_resampler_stream(
    fake_factory: FakeResamplerFactory,
) -> None:
    """Filter state from one rate must never be fed samples at another."""
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    pipeline.feed("mic", pcm_20ms(rate=48_000))
    pipeline.feed("mic", pcm_20ms(rate=44_100))

    assert fake_factory.created == [(48_000, 24_000), (44_100, 24_000)]


def test_unmuting_starts_a_fresh_resampler_stream(
    fake_factory: FakeResamplerFactory,
) -> None:
    """The filter tail held from before a mute must not color audio after it."""
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    pipeline.feed("mic", pcm_20ms())
    pipeline.skip("mic")
    pipeline.feed("mic", pcm_20ms())

    assert len(fake_factory.created) == 2


def test_pipeline_rejects_skipping_an_unknown_channel(
    fake_factory: FakeResamplerFactory,
) -> None:
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    with pytest.raises(ValueError, match="channel"):
        pipeline.skip("speaker")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "pcm",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"\x00\x00", id="two_bytes_is_an_absurd_rate"),
        pytest.param(b"\x00" * 1_921, id="odd_byte_count_is_not_pcm16"),
        pytest.param(b"\x00" * (192_000 * 20 // 1_000 * 2 + 2), id="rate_above_192khz"),
    ],
)
def test_pipeline_rejects_blocks_that_are_not_20ms_of_pcm16(
    fake_factory: FakeResamplerFactory, pcm: bytes
) -> None:
    pipeline = AudioPipeline(resampler_factory=fake_factory)

    with pytest.raises(ValueError, match="20 ms"):
        pipeline.feed("mic", pcm)


def _speech_band_pcm16(seconds: float, rate: int) -> numpy.ndarray:
    """A deterministic speech-band multitone with a syllable-rate envelope."""
    t = numpy.arange(int(rate * seconds)) / rate
    tones = sum(
        numpy.sin(2 * numpy.pi * frequency * t + phase)
        for frequency, phase in [
            (120, 0.1),
            (500, 2.1),
            (1_000, 0.7),
            (2_200, 1.9),
            (3_400, 0.4),
            (7_600, 1.1),
        ]
    )
    envelope = 0.55 + 0.45 * numpy.sin(2 * numpy.pi * 4.0 * t)
    return numpy.clip(tones / 6 * envelope * 20_000, -32_768, 32_767).astype("<i2")


def test_soxr_resampling_is_continuous_across_block_boundaries() -> None:
    """Block-wise streaming must match converting the whole signal at once.

    The per-block one-shot conversion this replaced measured ~31 dB SNR on
    this signal -- its filter restarted at every 20 ms boundary -- so the
    60 dB floor asserts the stateful stream, not a tuned threshold.
    """
    signal = _speech_band_pcm16(2.0, 48_000)
    block_samples = 48_000 * 20 // 1_000

    stream = SoxrResamplerFactory().create(48_000, 24_000)
    streamed = numpy.frombuffer(
        b"".join(
            stream.process(signal[start : start + block_samples].tobytes())
            for start in range(0, signal.size, block_samples)
        ),
        dtype="<i2",
    )
    reference = soxr.resample(signal, 48_000, 24_000, quality="HQ").astype("<i2")

    compared = min(streamed.size, reference.size)
    assert compared > reference.size * 0.9
    residual = reference[:compared].astype(numpy.float64) - streamed[:compared].astype(
        numpy.float64
    )
    signal_power = numpy.sum(reference[:compared].astype(numpy.float64) ** 2)
    snr_db = 10 * numpy.log10(signal_power / max(float(numpy.sum(residual**2)), 1e-12))
    assert snr_db > 60
