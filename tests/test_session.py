from __future__ import annotations

import asyncio
import threading

import pytest

from broccoli_desktop.capture import DeviceUnavailableError
from broccoli_desktop.events import EVENT_HISTORY_MAX, EventHub
from broccoli_desktop.models import AudioFrame, ConnectionState, UiEvent
from broccoli_desktop.protocol import decode_audio_frame
from broccoli_desktop.remote import RemoteFailure, RemoteProtocolError, RemoteRequestError
from broccoli_desktop.session import (
    AUDIO_QUEUE_MAX_FRAMES,
    FRAME_DURATION_MS,
    CaptureChoices,
    DesktopSessionController,
)
from tests.fakes import (
    FakeCaptureBackend,
    FakeClock,
    FakeListeningRemote,
    FakeLiveRemoteStream,
    FakeRemoteStream,
    FakeSessionRemote,
)


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


def frame_at(index: int) -> AudioFrame:
    return AudioFrame(channel="mic", offset_ms=index * FRAME_DURATION_MS, pcm=b"\x00\x01" * 1200)


async def capture_frames(controller: DesktopSessionController, count: int) -> None:
    """Hand frames over the way the capture thread does.

    Going through ``_schedule_forward`` keeps the loop hop under test instead of
    reaching past it into whatever the controller keeps them in.
    """
    for index in range(count):
        controller._schedule_forward(frame_at(index))
    await settle()


async def drain(stream: FakeLiveRemoteStream, *, expected: int) -> None:
    """Give the loop room until the socket has been handed every frame.

    Waiting on what the fake stream received, rather than on controller
    internals, is what keeps the ordering assertion about the real path.
    """
    for _ in range(2_000):
        if len(stream.frames) >= expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Only {len(stream.frames)} of {expected} frames reached the remote.")


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
async def test_start_new_builds_a_local_summary_without_fetching_or_patching_a_remote_title(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)

    summary = await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    assert summary.uuid_code == "session-1"
    assert summary.title == "Daily"
    assert summary.started_at is None
    assert summary.segment_count == 0
    assert controller.pipeline_base_offset_ms == 0

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
async def test_start_new_leaves_transcription_language_for_the_remote_service_to_detect(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)

    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    assert fake_remote.stream_languages == [""]

    await controller.stop()


@pytest.mark.asyncio
async def test_resume_reuses_the_current_session_summary_and_remote_uuid(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    started = await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    await fake_remote.emit_segment("system", "system:1", "first segment", 100, 900)
    await settle()
    await controller.stop()

    resumed = await controller.resume(started.uuid_code, CaptureChoices("mic-1", "system-1"))

    assert resumed.uuid_code == started.uuid_code
    assert resumed.title == "Daily"
    assert resumed.segment_count == 1
    assert fake_remote.stream_requests == [(None, "Speakers"), ("session-1", "Speakers")]
    assert controller.state is ConnectionState.STREAMING

    await controller.stop()


@pytest.mark.asyncio
async def test_update_title_changes_only_the_active_local_summary(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    started = await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    async def remote_title_update_must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("remote title updates are outside the Listening contract")

    fake_remote.update_title = remote_title_update_must_not_be_called  # type: ignore[method-assign]
    summary = await controller.update_title(started.uuid_code, "Renamed")

    assert summary.title == "Renamed"
    assert controller.session == summary
    assert fake_remote.sessions[started.uuid_code].title == "Daily"

    await controller.stop()


@pytest.mark.asyncio
async def test_final_segment_increments_the_local_active_session_count(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")

    await fake_remote.emit_segment("mic", "mic:1", "hello", 100, 900)
    await settle()

    assert controller.session is not None
    assert controller.session.segment_count == 1

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
    assert controller.pipeline_base_offset_ms == 0
    assert [decode_audio_frame(frame).offset_ms for frame in fake_remote.streams[-1].frames] == [
        100,
        300,
    ]
    assert controller.state is ConnectionState.STREAMING

    await controller.stop()


@pytest.mark.asyncio
async def test_recovery_keeps_local_offsets_when_backend_omits_next_offset_ms(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")
    controller.enqueue_audio_frames([AudioFrame("system", 600, b"\x00\x00")])

    await fake_remote.emit_failure()
    await settle()

    assert decode_audio_frame(fake_remote.streams[-1].frames[0]).offset_ms == 600

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
async def test_a_stop_landing_while_capture_is_still_opening_does_not_strand_streaming(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """CaptureSession.start() now opens its two WASAPI endpoints on a worker
    thread (asyncio.to_thread), freeing the loop for the rest of the open --
    including to a concurrent stop(). Without a check in _open(), a stop
    landing in that window nulls self._capture while start() is still
    running, and _open() -- unaware -- goes on to declare STREAMING with no
    capture behind it."""
    mic_opening = threading.Event()
    release_mic_open = threading.Event()
    real_open_microphone = fake_capture.open_microphone

    def slow_open_microphone(device_id: str, on_pcm):
        mic_opening.set()
        assert release_mic_open.wait(timeout=5), "test never released the open"
        return real_open_microphone(device_id, on_pcm)

    fake_capture.open_microphone = slow_open_microphone  # type: ignore[method-assign]

    controller = DesktopSessionController(fake_remote, fake_capture)
    start_task = asyncio.create_task(
        controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    )
    assert await asyncio.to_thread(mic_opening.wait, 5), (
        "capture.start() never reached open_microphone"
    )

    # stop() lands while capture.start() is still parked opening the second
    # (system) device. Its own asyncio.to_thread() hop lets its early nulling
    # of self._capture run on a worker thread without touching the lock
    # capture.start() is holding, so this poll -- not a fixed sleep -- is what
    # makes the race deterministic: it waits for exactly the state transition
    # under test instead of a guessed duration.
    stop_task = asyncio.create_task(controller.stop())
    for _ in range(10_000):
        if controller._capture is None:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("stop() never reclaimed self._capture before the open finished")

    release_mic_open.set()

    with pytest.raises(RuntimeError, match="stopped while it was starting"):
        await start_task
    await stop_task

    assert controller.state is ConnectionState.STOPPED
    assert controller._capture is None
    assert controller._stream is None
    assert controller._sender_task is None
    assert controller._reader_task is None


@pytest.mark.asyncio
async def test_sync_local_capture_stop_keeps_controller_stop_for_remote_finalization(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """Shutdown can synchronously silence local capture without bypassing controller stop."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    try:
        controller.stop_local_capture()

        assert fake_capture.closed_sources == {"mic-1", "system-1"}
        assert controller.state is ConnectionState.STREAMING
    finally:
        await controller.stop()

    assert fake_remote.streams[-1].controls == [{"type": "session.end"}]
    assert controller.state is ConnectionState.STOPPED


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


@pytest.mark.asyncio
async def test_capture_startup_failure_ends_the_started_remote_period_before_closing(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    fake_capture.fail_opening = "mic-1"
    controller = DesktopSessionController(fake_remote, fake_capture)
    controller.enqueue_audio_frames([AudioFrame("mic", 100, bytes(2))])

    with pytest.raises(DeviceUnavailableError):
        await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")

    assert fake_remote.streams[0].controls == [{"type": "session.end"}]
    assert fake_remote.streams[0].lifecycle == ["control", "close"]
    assert controller.buffered_audio_ms == 0
    assert controller.state is ConnectionState.DEVICE_SELECTION_REQUIRED


@pytest.mark.asyncio
async def test_startup_finalization_errors_do_not_mask_the_local_capture_failure(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    fake_capture.fail_opening = "mic-1"
    fake_remote.fail_control_stream_indexes = {0}
    fake_remote.fail_close_stream_indexes = {0}
    controller = DesktopSessionController(fake_remote, fake_capture)

    with pytest.raises(DeviceUnavailableError):
        await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")

    assert fake_remote.streams[0].lifecycle == ["control", "close"]


@pytest.mark.asyncio
async def test_startup_failure_before_session_started_does_not_send_session_end(
    fake_capture: FakeCaptureBackend,
) -> None:
    stream = FakeRemoteStream(scripted_events=[RemoteFailure()])
    remote = FakeListeningRemote(stream=stream)
    controller = DesktopSessionController(remote, fake_capture)

    with pytest.raises(RemoteProtocolError):
        await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")

    assert stream.controls == []
    assert stream.closed is True


@pytest.mark.asyncio
async def test_device_loss_ends_the_started_remote_period_before_closing(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")

    fake_capture.handles["system-1"].lose_device()
    await settle()

    assert fake_remote.streams[0].controls == [{"type": "session.end"}]
    assert fake_remote.streams[0].lifecycle == ["control", "close"]
    assert controller.state is ConnectionState.DEVICE_SELECTION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["stop", "remote_end", "remote_failure", "device_loss"])
async def test_terminal_paths_discard_buffered_frames_before_a_fresh_reconnect(
    terminal: str,
    fake_clock: FakeClock,
    fake_remote: FakeSessionRemote,
    fake_capture: FakeCaptureBackend,
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    choices = CaptureChoices("mic-1", "system-1")
    await controller.start_new(choices, title="")
    controller.enqueue_audio_frames([AudioFrame("mic", 100, bytes(2))])

    if terminal == "stop":
        await controller.stop()
    elif terminal == "remote_end":
        await fake_remote.emit_ended()
        await settle()
    elif terminal == "remote_failure":
        await fake_remote.emit_credit_denied()
        await settle()
    else:
        fake_capture.handles["system-1"].lose_device()
        await settle()

    assert controller.buffered_audio_ms == 0

    await controller.resume("session-1", choices)
    controller.enqueue_audio_frames([AudioFrame("system", 900, bytes(2))])
    await fake_remote.emit_failure()
    await settle()

    assert [decode_audio_frame(frame).offset_ms for frame in fake_remote.streams[-1].frames] == [
        900
    ]


@pytest.mark.asyncio
async def test_recovery_auth_failure_discards_buffered_frames_before_a_later_reconnect(
    fake_clock: FakeClock,
    fake_remote: FakeSessionRemote,
    fake_capture: FakeCaptureBackend,
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    choices = CaptureChoices("mic-1", "system-1")
    await controller.start_new(choices, title="")
    controller.enqueue_audio_frames([AudioFrame("mic", 100, bytes(2))])
    fake_remote.revoke_token()

    await fake_remote.emit_failure()
    await settle()

    assert controller.state is ConnectionState.FAILED
    assert controller.buffered_audio_ms == 0

    fake_remote.unauthorized = False
    await controller.resume("session-1", choices)
    controller.enqueue_audio_frames([AudioFrame("system", 900, bytes(2))])
    await fake_remote.emit_failure()
    await settle()

    assert [decode_audio_frame(frame).offset_ms for frame in fake_remote.streams[-1].frames] == [
        900
    ]


@pytest.mark.asyncio
async def test_recovery_exhaustion_discards_buffered_frames_before_a_later_reconnect(
    fake_clock: FakeClock,
    fake_remote: FakeSessionRemote,
    fake_capture: FakeCaptureBackend,
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    choices = CaptureChoices("mic-1", "system-1")
    original_connect = fake_remote.connect_stream

    async def fail_reconnects(
        *, resume_code: str | None, device_label: str, language: str, title: str | None = None
    ):
        if resume_code is not None:
            raise RemoteRequestError()
        return await original_connect(
            resume_code=resume_code, device_label=device_label, language=language, title=title
        )

    fake_remote.connect_stream = fail_reconnects  # type: ignore[method-assign]
    await controller.start_new(choices, title="")
    controller.enqueue_audio_frames([AudioFrame("mic", 100, bytes(2))])

    await fake_remote.emit_failure()
    await settle()

    assert controller.state is ConnectionState.FAILED
    assert controller.buffered_audio_ms == 0

    fake_remote.connect_stream = original_connect  # type: ignore[method-assign]
    await controller.resume("session-1", choices)
    controller.enqueue_audio_frames([AudioFrame("system", 900, bytes(2))])
    await fake_remote.emit_failure()
    await settle()

    assert [decode_audio_frame(frame).offset_ms for frame in fake_remote.streams[-1].frames] == [
        900
    ]


@pytest.mark.asyncio
async def test_a_stalled_socket_bounds_the_queue_instead_of_growing(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """Capture produces 20 frames/second/channel in real time. A socket that is
    slow rather than broken never reaches RECONNECTING, so the reconnect buffer
    never applies -- without a bound here, the frames just accumulate."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    fake_remote.streams[-1].block_sends()

    await capture_frames(controller, AUDIO_QUEUE_MAX_FRAMES * 3)

    assert controller._audio_queue.qsize() <= AUDIO_QUEUE_MAX_FRAMES
    assert controller._dropped_frames > 0

    await controller.stop()


@pytest.mark.asyncio
async def test_dropping_audio_tells_the_user_once_per_stall(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """Silent audio loss in a transcription product is worse than a visible gap,
    but one warning per dropped frame would bury the rest of the UI."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    fake_remote.streams[-1].block_sends()

    await capture_frames(controller, AUDIO_QUEUE_MAX_FRAMES * 2)

    warnings = [event for event in controller.events.snapshot() if event.type == "warning"]
    assert [event.message for event in warnings] == [
        "Áudio está sendo descartado: a conexão não está acompanhando."
    ]

    await controller.stop()


@pytest.mark.asyncio
async def test_frames_reach_the_remote_in_offset_order(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """One consumer means ordering by construction. With a task per frame they
    interleave at send()'s drain point and reach the socket out of order."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    stream = fake_remote.streams[-1]
    stream.stagger_sends()

    await capture_frames(controller, 20)
    await drain(stream, expected=20)

    assert [decode_audio_frame(frame).offset_ms for frame in stream.frames] == [
        index * FRAME_DURATION_MS for index in range(20)
    ]

    await controller.stop()


@pytest.mark.asyncio
async def test_stop_leaves_no_task_running(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    sender = controller._sender_task
    reader = controller._reader_task

    await controller.stop()

    assert sender is not None and sender.done()
    assert reader is not None and reader.done()
    assert controller._sender_task is None
    assert controller._reader_task is None
    assert not controller._tasks


@pytest.mark.asyncio
async def test_stop_during_a_stall_cancels_the_parked_sender_and_stays_idempotent(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """The sender is parked inside a send that will never return, which is
    exactly when a stop that waits on it would hang instead of finishing."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    fake_remote.streams[-1].block_sends()
    await capture_frames(controller, 4)
    sender = controller._sender_task

    await asyncio.wait_for(controller.stop(), timeout=1)
    await controller.stop()

    assert sender is not None and sender.done()
    assert not controller._tasks
    assert fake_remote.streams[-1].controls == [{"type": "session.end"}]
    assert controller.state is ConnectionState.STOPPED


@pytest.mark.asyncio
async def test_a_resumed_run_gets_one_sender_and_none_of_the_previous_audio(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """A second sender against the same queue would put ordering back in the
    hands of whichever task wins the drain, and audio captured under the
    previous run's offsets belongs to a session that is over.

    The stale frame is posted with no await before the resume, which is the
    only interesting way to post it: that leaves the capture thread's callback
    still sitting in the loop's ready queue when the new run swaps the queue
    out, so it lands in the new run's queue instead of the old one's.
    """
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    choices = CaptureChoices("mic-1", "system-1")
    await controller.start_new(choices, title="")
    fake_remote.streams[-1].block_sends()
    await capture_frames(controller, 4)
    first_sender = controller._sender_task

    await fake_remote.emit_credit_denied()
    await settle()
    assert controller.state is ConnectionState.FAILED

    controller._schedule_forward(frame_at(9_999))
    await controller.resume("session-1", choices)
    await settle()

    # cancelled(), not done(): a sender that unwound into an exception is done
    # too, and gather(return_exceptions=True) would swallow it.
    assert first_sender is not None and first_sender.cancelled()
    assert controller._sender_task is not first_sender
    assert fake_remote.streams[-1].frames == []

    await controller.stop()


def test_a_controller_reopened_on_a_second_event_loop_still_sends_audio(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """One controller outlives the loop a single run was opened on: every local
    API request drives its own. A queue held across runs would bind to the loop
    that is gone and take the next run's sender down with it, silently."""
    controller = DesktopSessionController(fake_remote, fake_capture)

    async def run_once() -> None:
        await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
        await capture_frames(controller, 3)
        await drain(fake_remote.streams[-1], expected=3)
        await controller.stop()

    asyncio.run(run_once())
    asyncio.run(run_once())

    assert [decode_audio_frame(frame).offset_ms for frame in fake_remote.streams[-1].frames] == [
        0,
        100,
        200,
    ]


@pytest.mark.asyncio
async def test_a_frame_the_previous_run_handed_over_late_never_reaches_the_new_one(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """A capture thread can hand a frame over after its own run is finished:
    CaptureSession.close() joins its worker with a timeout, so the join can
    return before the last callback has been posted. Nothing here waits for the
    loop between the hand-over and the next run streaming, which is the case a
    state check cannot catch -- by the time the frame is read, the state is the
    new run's and looks perfectly live."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    choices = CaptureChoices("mic-1", "system-1")
    await controller.start_new(choices, title="")
    await controller.stop()

    controller._schedule_forward(frame_at(9_999))
    await controller.start_new(choices, title="")
    await settle()

    assert fake_remote.streams[-1].frames == []

    await controller.stop()


def test_the_event_hub_does_not_grow_without_bound() -> None:
    """Every delta and every segment passes through here. Unbounded, a
    four-hour meeting keeps the whole transcript in memory for a snapshot
    nothing in the product reads."""
    hub = EventHub()

    for index in range(EVENT_HISTORY_MAX * 2):
        hub.publish(UiEvent(type="info", message=f"event {index}"))

    assert len(hub.snapshot()) == EVENT_HISTORY_MAX
    assert hub.snapshot()[-1].message == f"event {EVENT_HISTORY_MAX * 2 - 1}"
