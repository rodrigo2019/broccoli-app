from __future__ import annotations

import ast
import asyncio
import inspect
import threading

import pytest

import broccoli_desktop.session
from broccoli_desktop.capture import DeviceUnavailableError
from broccoli_desktop.events import EVENT_HISTORY_MAX, EventHub
from broccoli_desktop.i18n import SUPPORTED_UI_LOCALES, translate
from broccoli_desktop.models import AudioFrame, ConnectionState, SessionSummary, UiEvent
from broccoli_desktop.protocol import decode_audio_frame
from broccoli_desktop.remote import (
    CreditWarning,
    RemoteFailure,
    RemoteProtocolError,
    RemoteRequestError,
    SessionEnded,
    TranscriptDeltaEvent,
    TranscriptDiscardEvent,
    TranscriptSegmentEvent,
)
from broccoli_desktop.session import (
    AUDIO_QUEUE_MAX_FRAMES,
    FRAME_DURATION_MS,
    MAX_BUFFERED_AUDIO_MS,
    MAX_BUFFERED_FRAMES,
    RETRY_DELAYS_SECONDS,
    CaptureChoices,
    CaptureLanguages,
    DesktopSessionController,
)
from tests.fakes import (
    SESSION_STARTED_AT,
    FakeCaptureBackend,
    FakeClock,
    FakeListeningRemote,
    FakeLiveRemoteStream,
    FakeRemoteStream,
    FakeSessionRemote,
    settle,
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
    # These used to be asserted as None, which pinned a real defect as if it
    # were the contract: the history sorts by last activity and renders a date
    # from these fields, so a summary without them put the session the user had
    # just created at the very bottom of the list, undated, until a reload
    # replaced it. They come from the server's own session.started event now --
    # the client has no clock it can trust for a row the server just made.
    assert summary.started_at == SESSION_STARTED_AT
    assert summary.last_activity_at == SESSION_STARTED_AT
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
    """No forced language is the default: the service detects both channels."""
    controller = DesktopSessionController(fake_remote, fake_capture)

    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    assert fake_remote.stream_languages == [("", "")]

    await controller.stop()


@pytest.mark.asyncio
async def test_the_language_forced_on_each_channel_reaches_the_handshake(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)

    await controller.start_new(
        CaptureChoices("mic-1", "system-1"),
        title="Daily",
        languages=CaptureLanguages(microphone="pt", system="en"),
    )

    assert fake_remote.stream_languages == [("pt", "en")]
    assert controller.selected_languages == CaptureLanguages(microphone="pt", system="en")

    await controller.stop()


@pytest.mark.asyncio
async def test_a_reconnect_asks_for_the_languages_the_run_started_with(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend, fake_clock: FakeClock
) -> None:
    """A recovery that fell back to detection would change what is transcribed."""
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(
        CaptureChoices("mic-1", "system-1"),
        title="Daily",
        languages=CaptureLanguages(microphone="pt", system="en"),
    )

    await fake_remote.emit_failure()
    await settle()

    assert controller.state is ConnectionState.STREAMING
    assert fake_remote.stream_languages == [("pt", "en"), ("pt", "en")]

    await controller.stop()


@pytest.mark.asyncio
async def test_the_terminal_stream_a_stop_opens_keeps_the_run_languages(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend, fake_clock: FakeClock
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(
        CaptureChoices("mic-1", "system-1"),
        title="Daily",
        languages=CaptureLanguages(microphone="pt", system="en"),
    )
    # Leave the controller reconnecting with no stream of its own, which is the
    # one path where stop() has to open a connection to end the session.
    controller._stream = None
    controller.state = ConnectionState.RECONNECTING

    await controller.stop()

    # Two handshakes: the one that opened the run, and the one stop() had to
    # open to end a session it no longer held a stream for.
    assert fake_remote.stream_languages == [("pt", "en"), ("pt", "en")]


@pytest.mark.asyncio
async def test_a_resumed_session_carries_the_languages_it_was_resumed_with(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    started = await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    await controller.stop()

    await controller.resume(
        started.uuid_code,
        CaptureChoices("mic-1", "system-1"),
        languages=CaptureLanguages(microphone="en", system=""),
    )

    assert fake_remote.stream_languages == [("", ""), ("en", "")]

    await controller.stop()


def pcm_20ms_block() -> bytes:
    """One capture block, at the size the pipeline accepts."""
    return b"\x10\x00" * 960


@pytest.mark.asyncio
async def test_a_muted_channel_sends_nothing_while_the_other_keeps_streaming(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.set_channel_muted("mic", True)

    # Six blocks, not five: the continuous resampler holds back its filter
    # delay, so the first 100 ms frame completes one block after the naive
    # 5 x 20 ms arithmetic says it would.
    for _ in range(6):
        fake_capture.handles["mic-1"].emit(pcm_20ms_block())
        fake_capture.handles["system-1"].emit(pcm_20ms_block())
    await settle()

    sent = [decode_audio_frame(frame) for frame in fake_remote.streams[0].frames]
    assert [frame.channel for frame in sent] == ["system"]
    assert controller.muted_channels == {"mic": True, "system": False}

    await controller.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["mic", "system"])
async def test_mute_sends_one_flush_after_already_queued_audio(
    channel: str,
    fake_remote: FakeSessionRemote,
    fake_capture: FakeCaptureBackend,
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    stream = fake_remote.streams[-1]
    controller._enqueue_frame(AudioFrame(channel, 100, b"\x00\x00"), controller._run_generation)  # type: ignore[arg-type]

    controller.set_channel_muted(channel, True)  # type: ignore[arg-type]
    controller.set_channel_muted(channel, True)  # type: ignore[arg-type]
    controller.set_channel_muted(channel, False)  # type: ignore[arg-type]
    controller.set_channel_muted(channel, True)  # type: ignore[arg-type]
    await controller._audio_queue.join()

    assert [decode_audio_frame(frame).offset_ms for frame in stream.frames] == [100]
    assert stream.controls == [{"type": "channel.flush", "channel": channel}]
    await controller.stop()


@pytest.mark.asyncio
async def test_frame_scheduled_before_mute_is_revalidated_when_it_reaches_the_loop(
    fake_remote: FakeSessionRemote,
    fake_capture: FakeCaptureBackend,
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.set_channel_muted("mic", True)

    controller._enqueue_frame(AudioFrame("mic", 100, b"\x00\x00"), controller._run_generation)
    await controller._audio_queue.join()

    assert fake_remote.streams[-1].frames == []
    assert fake_remote.streams[-1].controls == [{"type": "channel.flush", "channel": "mic"}]
    await controller.stop()


@pytest.mark.asyncio
async def test_unmuting_resumes_where_the_meeting_is_rather_than_where_it_stopped(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """Offsets are positions in the meeting, so a mute has to spend them."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.set_channel_muted("mic", True)

    for _ in range(5):
        fake_capture.handles["mic-1"].emit(pcm_20ms_block())
    controller.set_channel_muted("mic", False)
    # Six blocks after the unmute -- see the muted-channel test above for why
    # the resampler's held filter delay costs the naive count one block.
    for _ in range(6):
        fake_capture.handles["mic-1"].emit(pcm_20ms_block())
    await settle()

    sent = [decode_audio_frame(frame) for frame in fake_remote.streams[0].frames]
    assert [(frame.channel, frame.offset_ms) for frame in sent] == [("mic", 100)]

    await controller.stop()


@pytest.mark.asyncio
async def test_a_muted_channel_meters_as_silence_so_the_histogram_shows_the_mute(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    levels: list[tuple[str, bytes]] = []
    controller = DesktopSessionController(
        fake_remote,
        fake_capture,
        on_audio_level=lambda channel, pcm: levels.append((channel, pcm)),
    )
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.set_channel_muted("mic", True)

    fake_capture.handles["mic-1"].emit(pcm_20ms_block())
    fake_capture.handles["system-1"].emit(pcm_20ms_block())

    assert levels == [("mic", bytes(1_920)), ("system", pcm_20ms_block())]

    await controller.stop()


@pytest.mark.asyncio
async def test_muting_an_unknown_channel_is_refused(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)

    with pytest.raises(ValueError, match="channel"):
        controller.set_channel_muted("speaker", True)  # type: ignore[arg-type]


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
async def test_resuming_the_open_session_reuses_the_in_memory_offset_without_a_round_trip(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """The base offset was only preserved for the session this controller
    already had open -- that in-memory path must keep working without a
    network round trip to the segment list."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    started = await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    await fake_remote.emit_segment("system", "system:1", "first segment", 100, 900)
    await settle()
    await controller.stop()

    async def must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("resuming the already-open session must not fetch segments")

    fake_remote.list_segments = must_not_be_called  # type: ignore[method-assign]
    fake_remote.get_session = must_not_be_called  # type: ignore[method-assign]

    resumed = await controller.resume(started.uuid_code, CaptureChoices("mic-1", "system-1"))

    assert resumed.segment_count == 1
    assert controller.pipeline_base_offset_ms == 0

    await controller.stop()


@pytest.mark.asyncio
async def test_resuming_a_stored_session_starts_after_its_last_segment(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """Opening a retained session from the library and pressing start gave
    base 0, because this controller has no in-memory history for a session it
    never had open -- so new speech was written at offsets that already held
    the earlier part of the meeting."""
    fake_remote.seed_segments("history-1", count=250, last_ended_offset_ms=1_240_000)
    controller = DesktopSessionController(fake_remote, fake_capture)

    resumed = await controller.resume("history-1", CaptureChoices("mic-1", "system-1"))

    assert controller.pipeline_base_offset_ms == 1_240_000
    # The count fetched to compute the offset above is reused here rather than
    # discarded: a library resume used to hardcode 0, showing "0 segmentos" in
    # the UI for a 250-segment meeting until new segments streamed in.
    assert resumed.segment_count == 250

    await controller.stop()


@pytest.mark.asyncio
async def test_resuming_a_stored_session_with_no_segments_makes_no_request(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    fake_remote.seed_segments("history-empty", count=0, last_ended_offset_ms=0)

    async def must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a session with no stored segments must not request a page")

    fake_remote.list_segments = must_not_be_called  # type: ignore[method-assign]
    controller = DesktopSessionController(fake_remote, fake_capture)

    await controller.resume("history-empty", CaptureChoices("mic-1", "system-1"))

    assert controller.pipeline_base_offset_ms == 0

    await controller.stop()


@pytest.mark.asyncio
async def test_a_stop_landing_during_the_library_resume_lookup_does_not_resurrect_the_run(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """The library-resume offset lookup added an await between connect_stream
    succeeding and this run touching any state stop() inspects. A stop()
    landing in that window used to find self._capture and self._stream still
    None -- both no-ops -- flip straight to STOPPED, and have nothing left to
    do; without a check, this run would then sail on to STREAMING right over
    it, and the remote stream it opened would never be told to end."""
    fake_remote.seed_segments("history-1", count=250, last_ended_offset_ms=1_240_000)
    controller = DesktopSessionController(fake_remote, fake_capture)

    entered_get_session = asyncio.Event()
    release_get_session = asyncio.Event()
    real_get_session = fake_remote.get_session

    async def blocking_get_session(uuid_code: str) -> SessionSummary:
        entered_get_session.set()
        await release_get_session.wait()
        return await real_get_session(uuid_code)

    fake_remote.get_session = blocking_get_session  # type: ignore[method-assign]

    resume_task = asyncio.create_task(
        controller.resume("history-1", CaptureChoices("mic-1", "system-1"))
    )
    await asyncio.wait_for(entered_get_session.wait(), timeout=1)
    assert controller.state is ConnectionState.STARTING

    await controller.stop()
    assert controller.state is ConnectionState.STOPPED
    assert controller._capture is None
    assert controller._stream is None

    release_get_session.set()

    with pytest.raises(RuntimeError, match="stopped while it was starting"):
        await asyncio.wait_for(resume_task, timeout=1)

    assert controller.state is ConnectionState.STOPPED
    assert controller._capture is None
    assert controller._stream is None
    assert fake_remote.streams[-1].controls == [{"type": "session.end"}]
    assert fake_remote.streams[-1].closed is True


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


def test_reconnect_buffer_covers_the_full_retry_backoff() -> None:
    """The buffer exists so a reconnect loses no speech.

    Both channels stream 100 ms frames concurrently, so wall-clock coverage
    is half the frame budget; it must exceed the whole backoff by a wide
    margin because each connect attempt adds its own time on top of the
    sleeps between attempts.
    """
    wall_coverage_ms = MAX_BUFFERED_FRAMES * FRAME_DURATION_MS // 2
    assert wall_coverage_ms >= sum(RETRY_DELAYS_SECONDS) * 1_000 * 5


@pytest.mark.asyncio
async def test_reconnect_buffer_trims_only_beyond_its_bound_and_warns_once(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """Losing buffered audio must be visible; losing it twice must not nag."""
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    warnings: list[UiEvent] = []
    controller.events.subscribe(
        lambda event: warnings.append(event) if event.type == "warning" else None
    )

    controller.enqueue_audio_frames(make_frames(milliseconds=MAX_BUFFERED_AUDIO_MS + 1_000))
    controller.enqueue_audio_frames(make_frames(milliseconds=1_000))

    assert controller.buffered_audio_ms == MAX_BUFFERED_AUDIO_MS
    assert len(warnings) == 1
    assert warnings[0].message == "notify.reconnect.trimmingAudio"

    await controller.stop()


@pytest.mark.asyncio
async def test_a_new_run_reports_its_own_reconnect_trim(
    fake_clock: FakeClock, fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    warnings: list[UiEvent] = []
    controller.events.subscribe(
        lambda event: warnings.append(event) if event.type == "warning" else None
    )

    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.enqueue_audio_frames(make_frames(milliseconds=MAX_BUFFERED_AUDIO_MS + 1_000))
    await controller.stop()
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.enqueue_audio_frames(make_frames(milliseconds=MAX_BUFFERED_AUDIO_MS + 1_000))

    assert len(warnings) == 2

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
async def test_mute_flush_survives_reconnection_and_follows_replayed_audio(
    fake_remote: FakeSessionRemote,
    fake_capture: FakeCaptureBackend,
) -> None:
    class GateClock:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def sleep(self, _delay: float) -> None:
            self.entered.set()
            await self.release.wait()

    clock = GateClock()
    controller = DesktopSessionController(fake_remote, fake_capture, clock=clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.enqueue_audio_frames([AudioFrame("mic", 100, b"\x00\x00")])

    await fake_remote.emit_failure()
    await clock.entered.wait()
    controller.set_channel_muted("mic", True)
    await controller._audio_queue.join()
    clock.release.set()
    await settle()

    reconnected = fake_remote.streams[-1]
    assert [decode_audio_frame(frame).offset_ms for frame in reconnected.frames] == [100]
    assert reconnected.controls == [{"type": "channel.flush", "channel": "mic"}]
    await controller.stop()


@pytest.mark.asyncio
async def test_discard_removes_only_the_matching_provisional_without_counting_a_segment(
    fake_remote: FakeSessionRemote,
    fake_capture: FakeCaptureBackend,
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    started = await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    stream = fake_remote.streams[-1]
    await stream.emit(TranscriptDeltaEvent("mic", "mic:item-1", "ruído", 100))
    await stream.emit(TranscriptDeltaEvent("system", "system:item-2", "fala", 200))
    await settle()

    await stream.emit(TranscriptDiscardEvent("mic", "mic:item-1", "empty"))
    await settle()

    assert set(controller.pending_deltas) == {"system:item-2"}
    assert controller.session is not None
    assert controller.session.segment_count == started.segment_count
    discards = [event.discard for event in controller.events.snapshot() if event.type == "discard"]
    assert [discard.utterance_id for discard in discards if discard is not None] == ["mic:item-1"]
    await controller.stop()


@pytest.mark.asyncio
async def test_frames_arriving_mid_replay_neither_skip_nor_drop_buffered_audio(
    fake_clock: FakeClock, fake_capture: FakeCaptureBackend
) -> None:
    """The replay used to walk the very list it was being appended to.

    While the state is RECONNECTING, the sender takes _forward_frame's
    RECONNECTING branch and calls enqueue_audio_frames, which extends, re-sorts
    and -- once the buffer is at its 10-second cap, which is exactly what a
    reconnect long enough to need a replay produces -- trims it with
    `del self._buffered_frames[:excess]`. Deleting from the front under a live
    `for` cursor shifts every remaining element left by one, so the loop steps
    straight over a frame; the clear() that followed then discarded whatever
    had been appended. Frames went missing with no counter and no warning.

    A full buffer plus one arrival at the first replay send is the smallest
    reproduction: with the old code offset 100 is sent, the arrival shifts the
    list, and offset 200 is never sent at all.

    Everything below is sized from MAX_BUFFERED_FRAMES -- the bound
    enqueue_audio_frames actually trims against -- and not from
    AUDIO_QUEUE_MAX_FRAMES, which equals it only because session.py sets them
    from the same expression. Sized off the queue's bound instead, decoupling
    the two would leave the buffer short of its cap, the arrival would stop
    triggering a trim, and this test would quietly start passing against the
    code it was written to fail against.
    """
    injected = [AudioFrame("system", (MAX_BUFFERED_FRAMES + 1) * 100, b"\x00\x00")]

    class InjectingStream:
        """Stand in for the sender reaching enqueue_audio_frames at one of the
        replay's awaits."""

        def __init__(self, inner: FakeLiveRemoteStream) -> None:
            self._inner = inner

        async def send_bytes(self, frame: bytes) -> None:
            if injected:
                controller.enqueue_audio_frames([injected.pop()])
            await self._inner.send_bytes(frame)

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    class InjectingRemote(FakeSessionRemote):
        async def connect_stream(self, **kwargs: object) -> object:
            stream = await super().connect_stream(**kwargs)  # type: ignore[arg-type]
            # Only the reconnect's stream: the first one is the live run.
            return InjectingStream(stream) if len(self.streams) > 1 else stream

    fake_remote = InjectingRemote()
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    buffered = [
        AudioFrame("system", offset_ms, b"\x00\x00")
        for offset_ms in range(100, (MAX_BUFFERED_FRAMES + 1) * 100, 100)
    ]
    controller.enqueue_audio_frames(buffered)
    # At the cap, not merely at a number that used to be the cap: the trim is
    # the skip mechanism, so a buffer with room left over reproduces nothing.
    # Asserted in both units the bound is expressed in, so a change to either
    # fails here instead of silently defusing the reproduction.
    assert len(buffered) == MAX_BUFFERED_FRAMES
    assert controller.buffered_audio_ms == MAX_BUFFERED_AUDIO_MS

    await fake_remote.emit_failure()
    await settle()

    replayed = [decode_audio_frame(frame).offset_ms for frame in fake_remote.streams[-1].frames]
    assert replayed == [frame.offset_ms for frame in buffered] + [(MAX_BUFFERED_FRAMES + 1) * 100]
    assert len(replayed) == len(set(replayed))
    assert replayed == sorted(replayed)
    assert controller.buffered_audio_ms == 0
    assert controller.state is ConnectionState.STREAMING

    await controller.stop()


# Two hops in flight: _handle_device_loss's capture teardown and settle's own.
@pytest.mark.parametrize("default_executor_workers", [4])
@pytest.mark.asyncio
async def test_a_buffer_cleared_during_a_replay_is_not_refilled_when_the_replay_unwinds(
    fake_clock: FakeClock, fake_capture: FakeCaptureBackend
) -> None:
    """Detaching the buffer to replay it cost one property the old cursor had.

    The old form walked the live list, so a concurrent _clear_buffered_frames()
    ended the loop and nothing came back. The detached form cannot see that
    clear at all -- it empties only the fresh list left behind -- so the
    `finally`, which exists to put an interrupted replay back for the next
    retry, put the frames into a buffer a device loss had just emptied on
    purpose. buffered_audio_ms then reported audio that no longer had a session
    to belong to, all the way through stop().
    """
    fake_remote = FakeSessionRemote()
    real_connect = fake_remote.connect_stream

    async def connect_and_park(**kwargs: object) -> FakeLiveRemoteStream:
        # The reconnect's socket accepts the connection and then never drains,
        # which parks the replay on its first send with the buffer detached --
        # the window this test needs to reach into.
        stream = await real_connect(**kwargs)  # type: ignore[arg-type]
        stream.block_sends()
        return stream

    fake_remote.connect_stream = connect_and_park  # type: ignore[method-assign]

    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    controller.enqueue_audio_frames(make_frames(300))
    assert controller.buffered_audio_ms == 300

    await fake_remote.emit_failure()
    for _ in range(2_000):
        if len(fake_remote.streams) == 2 and controller.buffered_audio_ms == 0:
            break
        await asyncio.sleep(0)
    assert len(fake_remote.streams) == 2, "the reconnect never opened its stream"
    assert fake_remote.streams[1].frames == [], "the replay was not parked on its first send"

    # The capture device disappears mid-replay. _handle_device_loss is a real
    # concurrent caller of _clear_buffered_frames while the state is
    # RECONNECTING, and it is the one the buffer was written to survive.
    fake_capture.handles["system-1"].lose_device()
    for _ in range(2_000):
        if controller.state is ConnectionState.DEVICE_SELECTION_REQUIRED:
            break
        await asyncio.sleep(0)
    assert controller.state is ConnectionState.DEVICE_SELECTION_REQUIRED
    assert controller.buffered_audio_ms == 0

    # stop() is what unwinds the parked replay: _cancel_tasks cancels the reader
    # driving it, and the cancel lands on the send, inside the try.
    await controller.stop()

    assert controller.buffered_audio_ms == 0


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
async def test_stop_during_reconnect_opens_one_terminal_stream_and_waits_for_ack(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    clock = BlockingClock()
    controller = DesktopSessionController(fake_remote, fake_capture, clock=clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    await fake_remote.emit_failure()
    await clock.entered.wait()
    await controller.stop()
    await settle()

    assert fake_remote.streams[0].controls == []
    assert fake_remote.streams[0].closed is True
    assert fake_remote.streams[1].controls == [{"type": "session.end"}]
    assert fake_remote.streams[1].closed is True
    assert fake_remote.stream_requests == [(None, "Speakers"), ("session-1", "Speakers")]
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


# Three thread hops have to be in flight at once here: the parked capture
# open, the test's own wait on it, and stop()'s teardown. The single-worker
# default executor that makes `settle` a barrier elsewhere would serialise
# them into a deadlock, so this test widens the pool for itself.
@pytest.mark.parametrize("default_executor_workers", [4])
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


# Two hops in flight: the parked capture teardown and the test's own wait on it.
@pytest.mark.parametrize("default_executor_workers", [4])
@pytest.mark.asyncio
async def test_capture_teardown_from_a_remote_end_does_not_freeze_the_event_loop(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """Only stop() used to hop off the loop.

    CaptureSession.stop() -> handle.close() joins two daemon workers with a
    one-second timeout each plus PortAudio teardown, and seven other teardown
    paths -- a remote end, a remote failure, a device loss, two in _recover and
    three in _open's except clauses -- ran it inline. Each one froze the event
    loop, and with it the transcript socket, the event feed and the level
    stream, for as long as the devices took to let go.

    Parking one handle's close is what makes that observable: while it is
    parked, an ordinary coroutine on this loop still has to get its turn.
    """
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    closing = threading.Event()
    release = threading.Event()
    handle = fake_capture.handles["mic-1"]
    inner_close = handle.close

    def blocking_close() -> None:
        closing.set()
        assert release.wait(timeout=5), "the test never released the close"
        inner_close()

    handle.close = blocking_close  # type: ignore[method-assign]

    await fake_remote.emit_ended()
    assert await asyncio.to_thread(closing.wait, 5), "teardown never reached handle.close"

    turns = 0
    for _ in range(5):
        await asyncio.sleep(0)
        turns += 1

    # Reached at all only because the loop was never the thread doing the join,
    # and the join really is still in progress: the parked close has not
    # returned, so the handle it belongs to is not closed yet.
    assert turns == 5
    assert handle.closed is False

    release.set()
    # The reader task is what runs _end_from_remote, and it returns once the
    # state has moved. Awaiting it is the exact barrier here; settle() is not,
    # because this test widened the executor and a to_thread hop through a
    # four-worker pool no longer orders behind the teardown job.
    reader = controller._reader_task
    assert reader is not None
    await reader

    assert controller.state is ConnectionState.STOPPED
    assert fake_capture.closed_sources == {"mic-1", "system-1"}


class ParkedCaptureTeardown:
    """Hold one capture handle's close() open.

    The teardown hop is only observable while it is still in progress: it is
    the window between "the loop is free again" and "this run is over" that the
    resurrection tests below are about. Parking a close is what holds that
    window open long enough to hand a frame through it.

    Patched at construction, before the event that triggers the teardown is
    emitted, because the patch has to be in place by the time the reader
    reaches it.
    """

    def __init__(self, fake_capture: FakeCaptureBackend) -> None:
        self._closing = threading.Event()
        self._release = threading.Event()
        handle = fake_capture.handles["mic-1"]
        inner_close = handle.close

        def blocking_close() -> None:
            self._closing.set()
            assert self._release.wait(timeout=5), "the test never released the close"
            inner_close()

        handle.close = blocking_close  # type: ignore[method-assign]

    async def wait_until_parked(self) -> None:
        parked = await asyncio.to_thread(self._closing.wait, 5)
        assert parked, "teardown never reached handle.close"

    def release(self) -> None:
        self._release.set()


async def offer_one_frame_during(controller: DesktopSessionController) -> None:
    """Hand a single frame over the way the capture thread does, then give the
    loop room for everything it could set in motion -- the sender's dequeue, a
    failing send, and the whole of _recover if the controller lets it start."""
    controller._schedule_forward(frame_at(0))
    for _ in range(500):
        await asyncio.sleep(0)


# Two hops in flight: the parked capture teardown and the test's own wait on it.
@pytest.mark.parametrize("default_executor_workers", [4])
@pytest.mark.asyncio
async def test_a_frame_offered_while_a_remote_end_tears_down_cannot_reopen_the_session(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend, fake_clock: FakeClock
) -> None:
    """Taking the capture teardown off the loop put a suspension point inside
    _end_from_remote, and the stream detach sat *after* it.

    For the ~2 seconds of the join the loop is free, the state still reads
    STREAMING and self._stream still points at the socket the backend has just
    closed. The sender dequeues a frame, sends on it, the send fails, and
    _forward_frame's handler calls _recover -- which has no state guard at
    entry, so it opens a *second* remote stream resuming a session the backend
    already ended. The window then says "Transmitindo" with no capture behind
    it: silent audio loss, which this file's own comments call its worst
    outcome.

    _handle_device_loss has always detached the stream before hopping, which is
    exactly why it was never exposed to this.
    """
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    parked = ParkedCaptureTeardown(fake_capture)
    # The socket the backend just closed: a send on it fails, which is what
    # sends _forward_frame into _recover.
    fake_remote.streams[0].fail_send = True
    await fake_remote.emit_ended()
    await parked.wait_until_parked()
    await offer_one_frame_during(controller)

    # One stream for the whole run: the frame found nothing to send on, so
    # nothing failed, so _recover was never reached.
    assert len(fake_remote.streams) == 1
    assert controller.state is not ConnectionState.RECONNECTING
    # ...because the detach happens before the hop, and the teardown it hopped
    # for is still parked right now.
    assert controller._stream is None
    assert controller._recovery_stream is None

    parked.release()
    reader = controller._reader_task
    assert reader is not None
    await reader

    assert controller.state is ConnectionState.STOPPED
    assert controller._stream is None
    assert len(fake_remote.streams) == 1


# Two hops in flight: the parked capture teardown and the test's own wait on it.
@pytest.mark.parametrize("default_executor_workers", [4])
@pytest.mark.asyncio
async def test_a_frame_offered_while_a_remote_failure_tears_down_cannot_reopen_the_session(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend, fake_clock: FakeClock
) -> None:
    """The same race as the remote-end case, through the other door.

    _fail_from_remote is what a credit denial, a duration limit and a remote
    failure all land in, and it had the same teardown-before-detach ordering.
    Resurrecting a session the backend refused to keep funding is worse than
    resurrecting one that merely ended.
    """
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    parked = ParkedCaptureTeardown(fake_capture)
    fake_remote.streams[0].fail_send = True
    await fake_remote.emit_credit_denied()
    await parked.wait_until_parked()
    await offer_one_frame_during(controller)

    assert len(fake_remote.streams) == 1
    assert controller.state is not ConnectionState.RECONNECTING
    assert controller._stream is None
    assert controller._recovery_stream is None

    parked.release()
    reader = controller._reader_task
    assert reader is not None
    await reader

    assert controller.state is ConnectionState.FAILED
    assert controller._stream is None
    assert len(fake_remote.streams) == 1


@pytest.mark.asyncio
async def test_a_second_concurrent_start_is_refused_rather_than_racing_the_first(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """The guard at the top of _open and the move to STARTING have to stay
    free of any await between them.

    _cancel_tasks already documents this -- it is deliberately placed after
    STARTING so that waiting on a previous run's sender cannot open a window
    for a second caller to walk past the guard. The device enumeration this
    method now takes has to obey the same rule, and it is the kind of line
    that reads as harmless setup and drifts upward.
    """
    controller = DesktopSessionController(fake_remote, fake_capture)
    first = asyncio.create_task(
        controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    )
    second = asyncio.create_task(
        controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    )
    results = await asyncio.gather(first, second, return_exceptions=True)

    refused = [result for result in results if isinstance(result, RuntimeError)]
    assert len(refused) == 1
    assert "already active" in str(refused[0])
    assert len(fake_remote.streams) == 1

    await controller.stop()


@pytest.mark.asyncio
async def test_the_audio_sender_survives_a_failure_outside_the_frame_handler(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """_send_audio_forever's try only wrapped _forward_frame.

    Anything raised outside it -- queue.get(), the per-iteration rebinding, a
    task_done() that disagrees with its queue -- killed the task for the rest of
    the run. The session then transmitted nothing while the interface still said
    "Transmitindo", with only a "never retrieved" line in the log to say so: a
    silently mute meeting, the worst outcome this file has.

    Both frames arriving is the discriminating assertion. The first is forwarded
    before task_done runs, so it lands either way; the second only lands if the
    loop survived to dequeue it.
    """
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    inner_task_done = controller._audio_queue.task_done
    calls: list[int] = []

    def exploding_task_done() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("task_done disagreed with its queue")
        inner_task_done()

    controller._audio_queue.task_done = exploding_task_done  # type: ignore[method-assign]

    await capture_frames(controller, 2)
    await drain(fake_remote.streams[-1], expected=2)

    sender = controller._sender_task
    assert sender is not None and not sender.done()
    assert [decode_audio_frame(frame).offset_ms for frame in fake_remote.streams[-1].frames] == [
        0,
        FRAME_DURATION_MS,
    ]

    await controller.stop()


#: The event types whose `message` the window renders. Everything else this
#: file publishes rides on `status`, whose message the interface never reads --
#: those stay English diagnostics.
_RENDERED_EVENT_TYPES = frozenset({"warning", "error", "recoverable_error"})


def _rendered_event_message_keys() -> set[str]:
    """Every catalog key a UiEvent in session.py can put in front of the user.

    Read out of the source rather than listed here, for the same reason the
    backend error details are: a hand-kept list is satisfied by editing this
    file, which is exactly what the next untranslated warning would do.
    """
    tree = ast.parse(inspect.getsource(broccoli_desktop.session))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "UiEvent":
            continue
        arguments = {
            keyword.arg: keyword.value.value
            for keyword in node.keywords
            if isinstance(keyword.value, ast.Constant)
        }
        if arguments.get("type") in _RENDERED_EVENT_TYPES and arguments.get("message"):
            keys.add(str(arguments["message"]))
    return keys


def test_every_message_this_file_can_show_is_translated_everywhere() -> None:
    """These strings reach a toast verbatim, so a key with no catalog entry is
    a toast that reads "notify.audio.dropping" -- in every language, including
    the one whoever added it was writing in."""
    keys = _rendered_event_message_keys()

    assert len(keys) >= 5
    for locale in SUPPORTED_UI_LOCALES:
        for key in keys:
            assert translate(locale, key) != key, (locale, key)


@pytest.mark.asyncio
async def test_the_low_credit_warning_reaches_the_interface_as_a_catalog_key(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    """`warning` is one of the two event types whose message the interface
    renders, so a literal string here is a string in one language on a window
    that can be in any of three. The key is the contract; the wording lives in
    the catalogs, once, beside every other piece of it."""
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    await fake_remote.streams[-1].emit(CreditWarning())
    await settle()

    warnings = [event for event in controller.events.snapshot() if event.type == "warning"]
    assert [event.message for event in warnings] == ["notify.credits.low"]

    await controller.stop()


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
async def test_stop_drains_audio_and_waits_for_the_final_segment_ack(
    fake_remote: FakeSessionRemote, fake_capture: FakeCaptureBackend
) -> None:
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    stream = fake_remote.streams[-1]
    stream.auto_end_ack = False
    controller._enqueue_frame(AudioFrame("mic", 100, b"\x00\x00"), controller._run_generation)

    stopping = asyncio.create_task(controller.stop())
    while not stream.controls:
        await asyncio.sleep(0)

    assert [decode_audio_frame(frame).offset_ms for frame in stream.frames] == [100]
    assert stopping.done() is False

    await stream.emit(TranscriptSegmentEvent("mic", "mic:item-final", "fim", 100, 200))
    await stream.emit(SessionEnded())
    await stopping

    segments = [event.segment for event in controller.events.snapshot() if event.type == "segment"]
    assert [segment.text for segment in segments if segment is not None] == ["fim"]
    assert controller.state is ConnectionState.STOPPED


@pytest.mark.asyncio
async def test_stop_closes_safely_when_finalization_ack_times_out(
    fake_remote: FakeSessionRemote,
    fake_capture: FakeCaptureBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import broccoli_desktop.session as session_module

    monkeypatch.setattr(session_module, "STOP_FINALIZE_TIMEOUT_SECONDS", 0.01)
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")
    fake_remote.streams[-1].auto_end_ack = False

    await controller.stop()

    assert fake_remote.streams[-1].controls == [{"type": "session.end"}]
    assert fake_remote.streams[-1].closed is True
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
        *,
        resume_code: str | None,
        device_label: str,
        language_mic: str,
        language_system: str,
        title: str | None = None,
    ):
        if resume_code is not None:
            raise RemoteRequestError()
        return await original_connect(
            resume_code=resume_code,
            device_label=device_label,
            language_mic=language_mic,
            language_system=language_system,
            title=title,
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
    assert [event.message for event in warnings] == ["notify.audio.dropping"]

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
