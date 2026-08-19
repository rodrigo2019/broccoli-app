from __future__ import annotations

import asyncio

import pytest

from broccoli_desktop.events import EventHub
from broccoli_desktop.models import AudioFrame, ConnectionState, UiEvent
from broccoli_desktop.protocol import decode_audio_frame
from broccoli_desktop.session import CaptureChoices, DesktopSessionController
from tests.fakes import FakeCaptureBackend, FakeClock, FakeSessionRemote


@pytest.fixture
def fake_remote() -> FakeSessionRemote:
    return FakeSessionRemote()


@pytest.fixture
def fake_capture() -> FakeCaptureBackend:
    return FakeCaptureBackend()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


def make_frames(milliseconds: int) -> list[AudioFrame]:
    return [
        AudioFrame("system", offset_ms, b"\x00\x00") for offset_ms in range(0, milliseconds, 100)
    ]


async def settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class BlockingClock:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def sleep(self, _delay: float) -> None:
        self.entered.set()
        await asyncio.Event().wait()


class SecondDelayClock:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self.second_delay_entered = asyncio.Event()

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        if delay == 2:
            self.second_delay_entered.set()
            await asyncio.Event().wait()


def test_event_hub_notifies_subscribers_and_retains_immutable_events() -> None:
    hub = EventHub()
    received: list[UiEvent] = []

    hub.subscribe(received.append)
    hub.publish(UiEvent(type="status", state=ConnectionState.STREAMING))
    hub.unsubscribe(received.append)
    hub.publish(UiEvent(type="status", state=ConnectionState.STOPPED))

    assert received == [UiEvent(type="status", state=ConnectionState.STREAMING)]
    assert hub.snapshot() == [
        UiEvent(type="status", state=ConnectionState.STREAMING),
        UiEvent(type="status", state=ConnectionState.STOPPED),
    ]
    with pytest.raises(AttributeError):
        hub.snapshot()[0].type = "warning"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_resume_uses_remote_offset_and_replaces_the_pending_delta(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    fake_remote.next_offsets = [45_000]
    controller = DesktopSessionController(fake_remote, fake_capture)

    await controller.resume("session-1", CaptureChoices("mic-1", "system-1"))
    await fake_remote.emit_delta("system", "utterance-1", "we should")
    await settle()
    await fake_remote.emit_segment("system", "utterance-1", "we should ship", 500, 1_000)
    await settle()

    events = controller.events.snapshot()
    assert controller.pipeline_base_offset_ms == 45_000
    assert events[-1].type == "segment"
    assert "utterance-1" not in controller.pending_deltas

    await controller.stop()


@pytest.mark.asyncio
async def test_start_new_opens_without_resume_code_and_updates_the_requested_title(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)

    summary = await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    assert fake_remote.stream_requests == [(None, "Speakers")]
    assert summary.title == "Daily"
    assert controller.state is ConnectionState.STREAMING

    await controller.stop()


@pytest.mark.asyncio
async def test_reconnect_discards_audio_beyond_ten_seconds(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    controller.enqueue_audio_frames(make_frames(milliseconds=11_000))

    assert controller.buffered_audio_ms == 10_000

    await controller.stop()


@pytest.mark.asyncio
async def test_reconnect_reopens_with_the_current_session_and_replays_buffered_frames_in_order(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    fake_remote.next_offsets = [0, 4_000]
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.enqueue_audio_frames(
        [
            AudioFrame("mic", 300, b"\x00\x00"),
            AudioFrame("system", 100, b"\x00\x00"),
        ]
    )

    await fake_remote.emit_failure()
    await settle()

    assert fake_clock.delays == [1]
    assert fake_remote.stream_requests == [(None, "Speakers"), ("session-1", "Speakers")]
    assert fake_remote.streams[0].closed is True
    assert controller.pipeline_base_offset_ms == 4_000
    assert [decode_audio_frame(frame).offset_ms for frame in fake_remote.streams[-1].frames] == [
        100,
        300,
    ]
    assert controller.state is ConnectionState.STREAMING

    await controller.stop()


@pytest.mark.asyncio
async def test_stop_during_reconnect_ends_the_logical_remote_session_without_retrying(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    clock = BlockingClock()
    controller = DesktopSessionController(fake_remote, fake_capture, clock=clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    await fake_remote.emit_failure()
    await clock.entered.wait()
    await controller.stop()
    await settle()

    assert fake_remote.streams[0].controls == [{"type": "session.end"}]
    assert fake_remote.streams[0].closed is True
    assert fake_remote.stream_requests == [(None, "Speakers")]
    assert controller.state is ConnectionState.STOPPED


@pytest.mark.asyncio
async def test_failed_replay_attempt_closes_its_stream_before_the_next_retry(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    fake_remote.fail_send_stream_indexes = {1}
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.enqueue_audio_frames([AudioFrame("system", 100, b"\x00\x00")])

    await fake_remote.emit_failure()
    await settle()

    assert fake_clock.delays == [1, 2]
    assert fake_remote.streams[1].closed is True
    assert fake_remote.streams[2].frames
    assert controller.state is ConnectionState.STREAMING

    await controller.stop()


@pytest.mark.asyncio
async def test_stop_after_a_failed_replay_reopens_only_to_end_the_remote_session(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    clock = SecondDelayClock()
    fake_remote.fail_send_stream_indexes = {1}
    controller = DesktopSessionController(fake_remote, fake_capture, clock=clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.enqueue_audio_frames([AudioFrame("system", 100, b"\x00\x00")])

    await fake_remote.emit_failure()
    await clock.second_delay_entered.wait()
    await controller.stop()
    await settle()

    assert clock.delays == [1, 2]
    assert fake_remote.streams[1].closed is True
    assert fake_remote.streams[2].controls == [{"type": "session.end"}]
    assert fake_remote.streams[2].closed is True
    assert controller.state is ConnectionState.STOPPED


@pytest.mark.asyncio
async def test_device_loss_during_reconnect_closes_the_retained_stream(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    clock = BlockingClock()
    controller = DesktopSessionController(fake_remote, fake_capture, clock=clock)
    await controller.resume("session-1", CaptureChoices("mic-1", "system-1"))

    await fake_remote.emit_failure()
    await clock.entered.wait()
    fake_capture.handles["system-1"].lose_device()
    await settle()

    assert fake_remote.streams[0].closed is True
    assert controller.state is ConnectionState.DEVICE_SELECTION_REQUIRED
    assert fake_remote.stream_requests == [("session-1", "Speakers")]


@pytest.mark.asyncio
async def test_stop_closes_capture_before_ending_the_remote_session(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    await controller.stop()

    assert fake_capture.closed_sources == {"mic-1", "system-1"}
    assert fake_remote.streams[-1].controls == [{"type": "session.end"}]
    assert controller.events.snapshot()[-1] == UiEvent(type="status", state=ConnectionState.STOPPED)


@pytest.mark.asyncio
async def test_session_end_stops_recovery_attempts(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.resume("session-1", CaptureChoices("mic-1", "system-1"))

    await fake_remote.emit_ended()
    await settle()

    assert fake_clock.delays == []
    assert controller.state is ConnectionState.STOPPED
    assert fake_remote.stream_requests == [("session-1", "Speakers")]


@pytest.mark.asyncio
async def test_remote_credit_denial_ends_capture_without_retrying(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.resume("session-1", CaptureChoices("mic-1", "system-1"))

    await fake_remote.emit_credit_denied()
    await settle()

    assert fake_clock.delays == []
    assert controller.state is ConnectionState.FAILED
    assert fake_capture.closed_sources == {"mic-1", "system-1"}


@pytest.mark.asyncio
async def test_device_loss_requires_device_selection_without_retrying(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.resume("session-1", CaptureChoices("mic-1", "system-1"))

    fake_capture.handles["system-1"].lose_device()
    await settle()

    assert fake_clock.delays == []
    assert controller.state is ConnectionState.DEVICE_SELECTION_REQUIRED
    assert fake_remote.stream_requests == [("session-1", "Speakers")]
