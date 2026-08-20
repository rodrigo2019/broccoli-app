from __future__ import annotations

from dataclasses import dataclass

import pytest

from broccoli_desktop.audio import AudioPipeline


def pcm_20ms() -> bytes:
    return b"\x01\x00" * 960


def silent_pcm_20ms() -> bytes:
    return bytes(1_920)


@dataclass
class FakeResampler:
    output_bytes: int = 960

    def resample(self, pcm: bytes, input_rate: int, output_rate: int) -> bytes:
        return b"\x02\x00" * (self.output_bytes // 2)


@pytest.fixture
def fake_resampler() -> FakeResampler:
    return FakeResampler()


def test_pipeline_emits_one_100ms_24khz_frame_after_five_capture_blocks(
    fake_resampler: FakeResampler,
) -> None:
    pipeline = AudioPipeline(resampler=fake_resampler)

    frames = [frame for _ in range(5) for frame in pipeline.feed("system", pcm_20ms())]

    assert len(frames) == 1
    assert frames[0].channel == "system"
    assert len(frames[0].pcm) == 4_800
    assert frames[0].offset_ms == 0


def test_pipeline_forwards_silence_to_the_remote_vad_without_creating_a_gap(
    fake_resampler: FakeResampler,
) -> None:
    pipeline = AudioPipeline(resampler=fake_resampler)

    frames = [frame for _ in range(5) for frame in pipeline.feed("mic", silent_pcm_20ms())]

    assert len(frames) == 1
    assert frames[0].offset_ms == 0


def test_pipeline_keeps_mic_and_system_accumulation_independent(
    fake_resampler: FakeResampler,
) -> None:
    pipeline = AudioPipeline(resampler=fake_resampler)

    mic_before = [frame for _ in range(4) for frame in pipeline.feed("mic", pcm_20ms())]
    system_frames = [frame for _ in range(5) for frame in pipeline.feed("system", pcm_20ms())]
    mic_after = pipeline.feed("mic", pcm_20ms())

    assert mic_before == []
    assert [frame.channel for frame in system_frames] == ["system"]
    assert [frame.channel for frame in mic_after] == ["mic"]


def test_interleaved_sources_share_the_same_capture_period_offsets(
    fake_resampler: FakeResampler,
) -> None:
    pipeline = AudioPipeline(resampler=fake_resampler, base_offset_ms=12_345)

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
    pipeline = AudioPipeline(resampler=FakeResampler(output_bytes=1_000))

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
    fake_resampler: FakeResampler,
) -> None:
    pipeline = AudioPipeline(resampler=fake_resampler, base_offset_ms=12_345)

    frames = [frame for _ in range(5) for frame in pipeline.feed("mic", pcm_20ms())]

    assert len(frames) == 1
    assert frames[0].offset_ms == 12_345


def test_pipeline_rejects_non_20ms_pcm_blocks(fake_resampler: FakeResampler) -> None:
    pipeline = AudioPipeline(resampler=fake_resampler)

    with pytest.raises(ValueError, match="20 ms"):
        pipeline.feed("mic", b"\x00\x00")
