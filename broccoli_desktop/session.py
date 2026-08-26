"""Live desktop session composition, transcript updates, and recovery."""

from __future__ import annotations

import asyncio
import logging
from bisect import insort
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from broccoli_desktop.audio import AudioPipeline
from broccoli_desktop.capture import (
    CaptureBackend,
    CaptureSession,
    DeviceUnavailableError,
    PcmCallback,
)
from broccoli_desktop.events import EventHub
from broccoli_desktop.models import (
    AudioFrame,
    CaptureEvent,
    ConnectionState,
    DeviceDescriptor,
    SessionSummary,
    TranscriptDelta,
    TranscriptDiscard,
    TranscriptSegment,
    UiEvent,
)
from broccoli_desktop.protocol import encode_audio_frame
from broccoli_desktop.remote import (
    CreditWarning,
    ListeningRemote,
    RemoteCreditError,
    RemoteDurationError,
    RemoteEvent,
    RemoteFailure,
    RemoteProtocolError,
    RemoteStream,
    RemoteUnauthorizedError,
    SessionEnded,
    SessionStarted,
    TranscriptDeltaEvent,
    TranscriptDiscardEvent,
    TranscriptSegmentEvent,
)

#: Ten minutes of 100 ms frames -- five minutes of wall clock with both
#: channels streaming. Sized for transcript completeness rather than
#: resources: the whole retry backoff below plus its connect attempts fits
#: many times over, and the ceiling is ~29 MB at 4.8 KB per frame. The old
#: ten-second bound silently threw away most of any reconnect longer than
#: its first retry.
MAX_BUFFERED_AUDIO_MS = 600_000
FRAME_DURATION_MS = 100
RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 15)
AUTO_DETECT_LANGUAGE = ""

#: The languages a capture may be forced into, alongside automatic detection.
#: ISO-639-1, which is the form the transcription service takes.
SUPPORTED_LANGUAGES = frozenset({AUTO_DETECT_LANGUAGE, "pt", "en", "de"})

#: The reconnect buffer's bound, in frames. Named rather than recomputed at the
#: one place that trims, so that a test about the trim can size its buffer from
#: the same constant the trim reads instead of from something that merely
#: happens to equal it.
MAX_BUFFERED_FRAMES = MAX_BUFFERED_AUDIO_MS // FRAME_DURATION_MS

#: The in-flight bound, deliberately equal to the reconnect buffer's: both are
#: the same frame budget, and if they drift apart one of them is wrong.
AUDIO_QUEUE_MAX_FRAMES = MAX_BUFFERED_FRAMES
STOP_FINALIZE_TIMEOUT_SECONDS = 20.0
STOP_DRAIN_TIMEOUT_SECONDS = 0.5

#: How long a superseded transcript reader gets to acknowledge cancellation
#: before its replacement stops waiting for it. Short: the reader parks on a
#: dead socket's iterator or a backoff sleep, both of which take a cancel
#: immediately; the bound only keeps a reader that somehow ignores one from
#: hanging recovery or stop().
READER_RETIRE_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger(__name__)


class SleepClock(Protocol):
    async def sleep(self, delay: float) -> None: ...


@dataclass(frozen=True)
class CaptureChoices:
    microphone_id: str
    system_device_id: str


@dataclass(frozen=True)
class CaptureLanguages:
    """The language each channel is transcribed in; empty means detect it.

    Separate from CaptureChoices because these are not device identities:
    they are not persisted with the selection, they are chosen per capture,
    and they reach the remote as handshake parameters rather than as
    anything the local capture reads.
    """

    microphone: str = AUTO_DETECT_LANGUAGE
    system: str = AUTO_DETECT_LANGUAGE


#: Nothing forced on either channel, which is what every capture did before
#: the choice existed. A shared instance because the value is frozen and it
#: is the default of three signatures below.
DETECT_LANGUAGES = CaptureLanguages()


@dataclass(frozen=True)
class _ChannelFlush:
    channel: Literal["mic", "system"]
    generation: int


type _OutboundItem = AudioFrame | _ChannelFlush


class _RunReclaimed(Exception):
    """Internal signal: a concurrent stop() already finished while _open was
    suspended on a network call, before this run had touched any state stop()
    inspects (`_capture`, `_stream`).

    Not a failure of this run -- the user's stop already won -- so it must
    not be reported through the normal FAILED/DEVICE_SELECTION_REQUIRED
    transitions the other `except` clauses in `_open` use. Caught by its own
    clause, which ends whatever remote stream this run opened and re-raises
    as the same `RuntimeError` the later capture-identity check already
    raises for the same underlying scenario, so callers see one consistent
    outcome regardless of which check caught it.
    """


class DesktopSessionController:
    """Coordinate capture, encoded audio transport, and local transcript events."""

    def __init__(
        self,
        remote: ListeningRemote,
        capture_backend: CaptureBackend,
        *,
        clock: SleepClock | Callable[[float], Awaitable[None]] | None = None,
        on_authentication_failure: Callable[[], None] | None = None,
        on_audio_level: PcmCallback | None = None,
        on_capture_state: Callable[[bool], None] | None = None,
    ) -> None:
        self._remote = remote
        self._capture_backend = capture_backend
        self._clock = clock
        self._on_authentication_failure = on_authentication_failure
        self._on_audio_level = on_audio_level
        self._on_capture_state = on_capture_state
        self.events = EventHub()
        self.state = ConnectionState.IDLE
        self.pending_deltas: dict[str, TranscriptDelta] = {}
        self._buffered_frames: list[AudioFrame] = []
        self._buffer_generation = 0
        self._capture: CaptureSession | None = None
        self._stream: RemoteStream | None = None
        self._recovery_stream: RemoteStream | None = None
        self._pipeline: AudioPipeline | None = None
        # Set alongside the pipeline by _set_pipeline; declared here so the
        # attribute exists for the whole object lifetime rather than appearing
        # at the first _open.
        self._pipeline_base_offset_ms = 0
        self._session: SessionSummary | None = None
        self._session_uuid: str | None = None
        self._choices: CaptureChoices | None = None
        # The languages this run opened with. Held for the whole run because
        # a reconnect and the terminal stream have to ask for the same ones:
        # a reconnect that fell back to detection would change what the
        # service transcribes, mid-meeting, with nothing on screen saying so.
        self._languages = CaptureLanguages()
        # Read from the capture thread on every block, written from the loop
        # when the user toggles. Two bools need no lock of their own: a
        # toggle landing between two blocks takes effect on the next one,
        # which is exactly what muting means.
        self._muted: dict[Literal["mic", "system"], bool] = {"mic": False, "system": False}
        self._pending_flush_channels: set[Literal["mic", "system"]] = set()
        self._reconnect_flush_channels: set[Literal["mic", "system"]] = set()
        self._device_label: str | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._audio_queue: asyncio.Queue[_OutboundItem] = asyncio.Queue(
            maxsize=AUDIO_QUEUE_MAX_FRAMES + 2
        )
        self._sender_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._dropped_frames = 0
        self._reported_dropping = False
        self._trimmed_frames = 0
        self._reported_trimming = False
        self._run_generation = 0
        self._recovery_active = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._device_lookup: Callable[[], Awaitable[list[DeviceDescriptor]]] | None = None
        self._session_ended_event = asyncio.Event()
        self._stopping = False
        self._produced_frames = 0
        self._sent_frames = 0

    @property
    def buffered_audio_ms(self) -> int:
        return len(self._buffered_frames) * FRAME_DURATION_MS

    @property
    def audio_diagnostics(self) -> dict[str, int]:
        """Non-sensitive counters that distinguish capture and network loss."""
        return {
            "produced_frames": self._produced_frames,
            "sent_frames": self._sent_frames,
            "queued_frames": self._audio_queue.qsize(),
            "reconnect_buffered_frames": len(self._buffered_frames),
            "dropped_frames": self._dropped_frames,
            "trimmed_frames": self._trimmed_frames,
        }

    @property
    def selected_devices(self) -> CaptureChoices | None:
        """Return the currently selected local capture device identities."""
        return self._choices

    @property
    def selected_languages(self) -> CaptureLanguages:
        """Return the languages the current or most recent run was opened with."""
        return self._languages

    @property
    def muted_channels(self) -> dict[str, bool]:
        """Return which channels are currently withheld from the remote."""
        return dict(self._muted)

    @property
    def session(self) -> SessionSummary | None:
        """Return the active local session summary without remote payload data."""
        return self._session

    @property
    def pipeline_base_offset_ms(self) -> int:
        if self._pipeline is None:
            return 0
        return self._pipeline_base_offset_ms

    async def start_new(
        self,
        choices: CaptureChoices,
        title: str,
        *,
        languages: CaptureLanguages = DETECT_LANGUAGES,
    ) -> SessionSummary:
        """Open a new remote session with a local capture title."""
        return await self._open(choices, resume_code=None, title=title, languages=languages)

    async def resume(
        self,
        uuid_code: str,
        choices: CaptureChoices,
        *,
        title: str | None = None,
        languages: CaptureLanguages = DETECT_LANGUAGES,
    ) -> SessionSummary:
        """Resume the selected remote session with a fresh local capture lifecycle."""
        return await self._open(choices, resume_code=uuid_code, title=title, languages=languages)

    def set_channel_muted(self, channel: Literal["mic", "system"], muted: bool) -> None:
        """Withhold one channel's audio from the remote, or send it again.

        Muting drops the channel's frames rather than closing its device:
        the toggle has to be instant mid-meeting, and reopening a WASAPI
        endpoint is not. The offsets keep advancing while muted (see
        AudioPipeline.skip), so unmuting resumes at the point the meeting
        has actually reached instead of on top of transcript already there.
        """
        if channel not in self._muted:
            raise ValueError("Unknown audio channel.")
        muted = bool(muted)
        was_muted = self._muted[channel]
        self._muted[channel] = muted
        if (
            muted
            and not was_muted
            and self.state
            in {ConnectionState.STARTING, ConnectionState.STREAMING, ConnectionState.RECONNECTING}
        ):
            self._enqueue_flush(channel, self._run_generation)

    async def stop(self) -> None:
        """Drain captured audio and wait for the backend's final transcript."""
        if self.state is ConnectionState.STOPPED:
            return
        # CaptureSession.start() owns real OS locks. Keep its stop off the event
        # loop so the transcript reader can continue receiving the final turn.
        await asyncio.to_thread(self.stop_local_capture)
        self._stopping = True
        self._session_ended_event.clear()
        stream = self._stream or self._recovery_stream
        try:
            try:
                await asyncio.wait_for(
                    self._audio_queue.join(),
                    timeout=STOP_DRAIN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning("[session] Timed out draining the local audio queue")
                sender, self._sender_task = self._sender_task, None
                if sender is not None and not sender.done():
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)

            if self.state is ConnectionState.RECONNECTING:
                # The reader that drove this run into RECONNECTING is still
                # parked in its backoff sleep or on the dead socket; retire it
                # before the terminal reader takes the handle, or it runs on
                # orphaned where _cancel_tasks can no longer see it.
                await self._retire_reader(self._reader_task)
                self._reader_task = None
                await self._close_stream(stream)
                stream = await self._connect_terminal_stream()
                if stream is not None:
                    iterator = stream.events()
                    started = await anext(iterator)
                    if not isinstance(started, SessionStarted):
                        raise RemoteProtocolError("Terminal stream did not resume the session.")
                    self._stream = stream
                    self._recovery_stream = None
                    self._reader_task = asyncio.create_task(self._listen(iterator))
                    buffered, self._buffered_frames = self._buffered_frames, []
                    for frame in buffered:
                        await stream.send_bytes(
                            encode_audio_frame(
                                channel=frame.channel,
                                offset_ms=frame.offset_ms,
                                pcm=frame.pcm,
                            )
                        )
                        self._sent_frames += 1
                    await self._send_pending_flushes(stream)

            if stream is not None:
                await stream.send_control({"type": "session.end"})
                try:
                    await asyncio.wait_for(
                        self._session_ended_event.wait(),
                        timeout=STOP_FINALIZE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.warning("[session] Backend did not acknowledge finalization in time")
        except Exception:
            logger.exception("[session] Graceful remote finalization failed")
        finally:
            self._stream = None
            self._recovery_stream = None
            await self._cancel_tasks()
            if stream is not None:
                await self._close_stream(stream)
            self._clear_buffered_frames()
            logger.info("[session] Audio diagnostics at stop: %s", self.audio_diagnostics)
            self._reset_audio_queue()
            self._stopping = False
            self._set_state(ConnectionState.STOPPED)

    def stop_local_capture(self) -> None:
        """Stop capture synchronously while controller finalization remains pending.

        Stays synchronous on purpose: this is also the tray and console-shutdown
        teardown path (see runtime.py), which calls it from a plain background
        thread with no event loop to hop onto. Every caller that *does* have a
        loop goes through _stop_capture_off_loop instead.
        """
        self._stop_capture()

    def restore_selected_devices(self, choices: CaptureChoices) -> None:
        """Restore a previously validated local selection without starting capture."""
        self._choices = choices

    def set_remote(self, remote: ListeningRemote) -> None:
        """Point this controller at a freshly built transport.

        Used when the proxy settings change: the remote has to be rebuilt to
        pick up the new route, but this controller has to survive it, because
        every open /api/events socket is subscribed to *this* object's
        EventHub. Refused while a run is in flight -- that run holds a stream
        opened through the old transport, and swapping it underneath would
        leave the two disagreeing about where the audio goes.
        """
        if self.state in {
            ConnectionState.STARTING,
            ConnectionState.STREAMING,
            ConnectionState.RECONNECTING,
        }:
            raise RuntimeError("An active capture owns the remote connection.")
        self._remote = remote

    def clear_selected_devices(self) -> None:
        """Forget an idle local selection after the user restores default settings."""
        if self.state in {
            ConnectionState.STARTING,
            ConnectionState.STREAMING,
            ConnectionState.RECONNECTING,
        }:
            raise RuntimeError("An active capture owns the selected devices.")
        self._choices = None

    def set_device_lookup(self, lookup: Callable[[], Awaitable[list[DeviceDescriptor]]]) -> None:
        """Use the API layer's cached, off-loop enumeration instead of the backend.

        Services.list_devices caches for a few seconds and runs the call on a
        worker thread. Without this the controller called
        backend.list_devices() directly on the event loop, defeating that cache
        on the very path it was written to protect.
        """
        self._device_lookup = lookup

    def set_authentication_failure_handler(self, callback: Callable[[], None] | None) -> None:
        """Set the narrow local notification used when background recovery loses auth."""
        self._on_authentication_failure = callback

    def set_audio_level_callbacks(
        self,
        on_audio_level: PcmCallback | None,
        on_capture_state: Callable[[bool], None] | None,
    ) -> None:
        """Attach transient level reporting without retaining or forwarding PCM."""
        self._on_audio_level = on_audio_level
        self._on_capture_state = on_capture_state

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary:
        """Apply a remotely persisted title to the active local capture summary."""
        if self._session is None or self._session_uuid != uuid_code:
            raise RemoteProtocolError("The requested local session is not active.")
        summary = replace(self._session, title=title)
        self._session = summary
        self.events.publish(UiEvent(type="session", session=summary))
        return summary

    def apply_session_metadata(self, session: SessionSummary) -> SessionSummary:
        """Synchronize metadata returned by the remote API without replacing live state."""
        if self._session is None or self._session_uuid != session.uuid_code:
            return session
        summary = replace(
            self._session,
            title=session.title,
            is_pinned=session.is_pinned,
            pinned_at=session.pinned_at,
        )
        self._session = summary
        self.events.publish(UiEvent(type="session", session=summary))
        return summary

    def enqueue_audio_frames(self, frames: Iterable[AudioFrame]) -> None:
        """Keep a bounded, offset-ordered reconnect buffer of encoded frames.

        Ordered by insertion, never by re-sorting: the RECONNECTING path hands
        frames over one at a time, ~40 a second, and a full sort per frame over
        a buffer this deep is minutes of accumulated event-loop stalls -- long
        enough to miss keepalive pongs and turn one reconnect into a storm of
        them. In-order arrivals (the overwhelming case) append in O(1); a
        genuinely out-of-order frame is insorted after its equals, which is
        the position the old stable sort gave it. Every consumer -- the
        recovery replay, its ``finally`` re-enqueue, and stop()'s terminal
        replay -- relies on the order this method maintains.

        A trim is audio the transcript will never get back, so the first one
        of each run is announced. Once per run, like the in-flight drop
        warning: the situation persisting is not news, and the counter keeps
        the full extent for diagnostics.
        """
        buffered = self._buffered_frames
        for frame in frames:
            if not buffered or buffered[-1].offset_ms <= frame.offset_ms:
                buffered.append(frame)
            else:
                insort(buffered, frame, key=lambda queued: queued.offset_ms)
        excess = len(buffered) - MAX_BUFFERED_FRAMES
        if excess > 0:
            del buffered[:excess]
            self._trimmed_frames += excess
            if not self._reported_trimming:
                self._reported_trimming = True
                self.events.publish(
                    UiEvent(type="warning", message="notify.reconnect.trimmingAudio")
                )

    async def _open(
        self,
        choices: CaptureChoices,
        *,
        resume_code: str | None,
        title: str | None,
        languages: CaptureLanguages = DETECT_LANGUAGES,
    ) -> SessionSummary:
        if self.state in {
            ConnectionState.STARTING,
            ConnectionState.STREAMING,
            ConnectionState.RECONNECTING,
        }:
            raise RuntimeError("A desktop session is already active.")
        previous_session = (
            self._session if resume_code and self._session_uuid == resume_code else None
        )
        self._clear_buffered_frames()
        self.pending_deltas.clear()
        self._session_ended_event = asyncio.Event()
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._choices = choices
        self._languages = languages
        self._set_state(ConnectionState.STARTING)
        # After STARTING, so that waiting on a previous run's sender cannot open
        # a window for a second caller to walk past the guard above. A run that
        # ended without stop() -- a device loss, a failed reconnect -- still owns
        # a sender, and two senders on one queue would put ordering back in the
        # hands of whichever task wins the drain. Cancel before resetting, the
        # order stop() uses: the old sender has to finish with the queue it read
        # from before that queue is replaced.
        await self._cancel_tasks()
        self._reset_audio_queue()
        # Captured after the bump above, not before: this is the generation
        # this run owns. A concurrent stop() bumps it again (its own
        # _reset_audio_queue() call) strictly before it can reach STOPPED, so
        # a mismatch below reliably means stop() already finished tearing
        # down a run that, from its perspective, had nothing to tear down yet
        # -- self._stream and self._capture are both still unset at that
        # check, exactly where stop() looks.
        run_generation = self._run_generation
        # One enumeration for this whole start, from the cache when the API
        # layer gave this controller its lookup. It used to be three: the label
        # resolution below and CaptureSession's constructor both went straight
        # to the backend on the event loop, and CaptureSession.start() took a
        # third on its worker thread.
        #
        # Placed here, not up beside self._choices where the label used to be
        # resolved synchronously: an await between the guard at the top of this
        # method and _set_state(STARTING) would let a second caller walk past
        # that guard, which is the very window the comment above _cancel_tasks
        # exists to keep shut. After the generation capture as well, so a
        # concurrent stop() landing during this enumeration is caught by the
        # _RunReclaimed check rather than masked by a generation read taken
        # after the fact.
        device_labels = {device.device_id: device.label for device in await self._list_devices()}
        self._device_label = device_labels.get(choices.system_device_id, choices.system_device_id)
        stream = None
        remote_period_started = False
        try:
            stream = await self._remote.connect_stream(
                resume_code=resume_code,
                device_label=self._device_label,
                language_mic=languages.microphone,
                language_system=languages.system,
                title=title,
            )
            iterator = stream.events()
            started = await anext(iterator)
            if not isinstance(started, SessionStarted):
                raise RemoteProtocolError("Remote stream did not start a session.")
            remote_period_started = True
            if resume_code is not None and started.uuid_code != resume_code:
                raise RemoteProtocolError("Remote resumed an unexpected session.")
            previous_offset_ms, resumed_segment_count = await self._resume_state(
                previous_session, resume_code
            )
            if self._run_generation != run_generation:
                # A concurrent stop() finished while the line above was
                # awaiting the network -- see _RunReclaimed. Checked here,
                # before this run touches any state stop() looks at, rather
                # than only relying on the capture-identity check further
                # down: that check exists for the thread-hop window below,
                # not this one, and letting this run construct a
                # CaptureSession and open real WASAPI devices first just to
                # discard them would be needless work on a run that has
                # already lost.
                raise _RunReclaimed
            self._set_pipeline(previous_offset_ms)
            summary = SessionSummary(
                uuid_code=started.uuid_code,
                title=title
                if title is not None
                else (previous_session.title if previous_session else ""),
                status="live",
                # Prefer what the server just stamped. A brand-new session has
                # no previous_session to inherit from, so this used to be None
                # for exactly the row the user had just created: the history
                # sorts by activity, an absent timestamp parses to 0, and the
                # new session sank to the bottom of the list and rendered with
                # no date until the next reload put it right.
                started_at=started.started_at
                or (previous_session.started_at if previous_session else None),
                ended_at=None,
                last_activity_at=started.last_activity_at,
                device_label=self._device_label,
                segment_count=resumed_segment_count,
                is_live=True,
            )
            self._session = summary
            self._session_uuid = started.uuid_code
            self._stream = stream
            capture = CaptureSession(
                self._capture_backend,
                choices.microphone_id,
                choices.system_device_id,
                self._on_pcm,
                on_event=self._on_capture_event,
                on_audio_level=self._report_audio_level,
                on_capture_state=self._on_capture_state,
                device_labels=device_labels,
            )
            self._capture = capture
            # Opens two WASAPI endpoints synchronously; off the loop so a
            # session start cannot freeze the transcript socket and the meter
            # stream while Windows takes its time handing back the streams.
            # The loop is free for the rest of this open, including to a
            # concurrent stop() -- which is why this keeps its own reference
            # to `capture` rather than only reading it back off `self` below.
            await asyncio.to_thread(capture.start)
        except _RunReclaimed:
            # No capture teardown/_clear_buffered_frames()/_set_state() here,
            # unlike the clauses below: stop() already did all three (that is
            # what the generation bump we just detected means), and this run
            # never got far enough to touch any of that state itself --
            # self._capture, self._session and self._stream are all still
            # whatever they were before this open began. All that is left
            # for this run to clean up is the remote stream it opened, which
            # stop() could not have known about.
            await self._close_open_stream(stream, remote_period_started)
            raise RuntimeError("The session was stopped while it was starting.") from None
        except DeviceUnavailableError:
            await self._close_open_stream(stream, remote_period_started)
            await self._stop_capture_off_loop()
            self._clear_buffered_frames()
            self._set_state(ConnectionState.DEVICE_SELECTION_REQUIRED)
            raise
        except RemoteUnauthorizedError:
            await self._close_open_stream(stream, remote_period_started)
            await self._stop_capture_off_loop()
            self._clear_buffered_frames()
            self._set_state(ConnectionState.FAILED, message="Authentication failed.")
            raise
        except Exception:
            await self._close_open_stream(stream, remote_period_started)
            await self._stop_capture_off_loop()
            self._clear_buffered_frames()
            self._set_state(ConnectionState.FAILED, message="Unable to start the session.")
            raise
        if self._capture is not capture:
            # The second of two concurrent-stop checks in this method -- see
            # _RunReclaimed above for the first, earlier one. Different
            # mechanism on purpose: capture.start() succeeded, but
            # self._capture no longer points at the CaptureSession this run
            # just opened, meaning a concurrent stop() reclaimed the run
            # while it was opening. Clearing self._capture (and stopping
            # this exact object) is the very first thing stop() does, off its
            # own thread, so this can be true well before stop()
            # finishes -- a run-generation counter bumped later in stop()
            # would not reliably catch it *at this specific point*, which is
            # why this checks capture identity instead. (The earlier
            # generation check is reliable at its own, earlier point, before
            # self._capture is touched at all -- see _RunReclaimed.)
            # Declaring STREAMING now would
            # resurrect a session stop() is already tearing down, and starting
            # a sender/reader pair here would run them straight into the first
            # send() failure against a stream stop() is closing, with nobody
            # watching. stop() owns the rest of this run's teardown -- there is
            # nothing left here to do but tell the caller the start did not
            # happen.
            raise RuntimeError("The session was stopped while it was starting.")
        self.events.publish(UiEvent(type="session", session=summary))
        self._set_state(ConnectionState.STREAMING, session=summary)
        # Started here, not beside `self._stream = stream`, so that an open which
        # fails on the way to STREAMING cannot leave a sender running against a
        # stream nobody owns any more.
        self._sender_task = self._track(asyncio.create_task(self._send_audio_forever()))
        self._reader_task = asyncio.create_task(self._listen(iterator))
        return summary

    async def _listen(self, iterator: AsyncIterator[RemoteEvent]) -> None:
        try:
            async for event in iterator:
                await self._handle_remote_event(event)
                if (isinstance(event, RemoteFailure) and not self._stopping) or self.state not in {
                    ConnectionState.STREAMING,
                    ConnectionState.RECONNECTING,
                }:
                    return
        except asyncio.CancelledError:
            return
        except RemoteUnauthorizedError:
            await self._fail_from_remote("Authentication failed.")
            self._notify_authentication_failure()
        except RemoteCreditError:
            await self._fail_from_remote("Session credits are unavailable.")
        except RemoteDurationError:
            await self._fail_from_remote("Session reached its maximum duration.")
        except Exception:
            if self.state is ConnectionState.STREAMING:
                await self._recover()

    async def _handle_remote_event(self, event: RemoteEvent) -> None:
        if isinstance(event, TranscriptDeltaEvent):
            delta = TranscriptDelta(
                channel=self._channel(event.channel),
                utterance_id=event.utterance_id,
                text=event.text,
                started_offset_ms=event.started_offset_ms,
            )
            self.pending_deltas[delta.utterance_id] = delta
            self.events.publish(UiEvent(type="delta", delta=delta))
            return
        if isinstance(event, TranscriptDiscardEvent):
            discard = TranscriptDiscard(
                channel=self._channel(event.channel),
                utterance_id=event.utterance_id,
                reason=event.reason,
            )
            self.pending_deltas.pop(discard.utterance_id, None)
            self.events.publish(UiEvent(type="discard", discard=discard))
            return
        if isinstance(event, TranscriptSegmentEvent):
            segment = TranscriptSegment(
                utterance_id=event.utterance_id,
                channel=self._channel(event.channel),
                text=event.text,
                started_offset_ms=event.started_offset_ms,
                ended_offset_ms=event.ended_offset_ms,
            )
            self.pending_deltas.pop(segment.utterance_id, None)
            if self._session is not None:
                self._session = replace(
                    self._session, segment_count=self._session.segment_count + 1
                )
            self.events.publish(UiEvent(type="segment", segment=segment))
            return
        if isinstance(event, CreditWarning):
            # A catalog key, like every other message this file publishes for
            # the user to read: the interface owns the wording in all three
            # languages, and this process has no business holding a fourth copy
            # of it. `warning` and `error` are the only event types whose
            # `message` the interface renders (see handleEvent in app.js). The
            # English strings that remain in this file ride on `status` events,
            # whose `message` the interface never reads; they are diagnostics,
            # and the interface derives its own text from the state enum.
            self.events.publish(UiEvent(type="warning", message="notify.credits.low"))
            return
        if isinstance(event, SessionEnded):
            if self._stopping:
                self._session_ended_event.set()
                return
            await self._end_from_remote()
            return
        if isinstance(event, RemoteFailure):
            if self._stopping:
                self.events.publish(
                    UiEvent(type="error", message="notify.transcript.mayBeIncomplete")
                )
                return
            await self._fail_from_remote()

    async def _recover(self) -> None:
        if (
            self._stopping
            or self._recovery_active
            or self._session_uuid is None
            or self._choices is None
        ):
            return
        self._recovery_active = True
        failed_stream = self._stream
        self._stream = None
        self._recovery_stream = failed_stream
        self._set_state(
            ConnectionState.RECONNECTING, message="Connection interrupted. Reconnecting."
        )
        try:
            for delay in RETRY_DELAYS_SECONDS:
                await self._sleep(delay)
                if self._stopping or self.state is not ConnectionState.RECONNECTING:
                    return
                stream: RemoteStream | None = None
                try:
                    stream = await self._remote.connect_stream(
                        resume_code=self._session_uuid,
                        device_label=self._device_label or "",
                        language_mic=self._languages.microphone,
                        language_system=self._languages.system,
                        title=None,
                    )
                    previous_stream = self._recovery_stream
                    self._recovery_stream = stream
                    if previous_stream is not None:
                        await self._close_stream(previous_stream)
                    iterator = stream.events()
                    started = await anext(iterator)
                    if not isinstance(started, SessionStarted):
                        raise RemoteProtocolError("Remote stream did not start a session.")
                    if started.uuid_code != self._session_uuid:
                        raise RemoteProtocolError("Remote resumed an unexpected session.")
                    self._stream = stream
                    self._recovery_stream = None
                    # Detach the buffer before replaying it. The state is still
                    # RECONNECTING here, and at every await below the sender
                    # task -- still running, because this recovery can be
                    # driven by the reader -- can dequeue a frame, take
                    # _forward_frame's RECONNECTING branch and call
                    # enqueue_audio_frames, which inserts into and trims the
                    # list. Mutating under a `for` cursor skips and repeats
                    # elements, and the clear() that used to follow discarded
                    # whatever had been appended during the loop: frames left
                    # out of order and some never left at all, with no counter
                    # and no warning -- the opposite of the
                    # ordering-by-construction this buffer exists for.
                    #
                    # Draining until nothing new has arrived is what closes the
                    # handover without a gap. It terminates because each pass
                    # only carries what the capture produced during the
                    # previous one, at 20 frames per second per channel against
                    # a socket that just completed a handshake; a socket too
                    # slow for that never converges anywhere, and stop()
                    # cancels the task this runs in.
                    #
                    # The generation is read before the detach so the `finally`
                    # can tell "nothing else touched the buffer" from "another
                    # path emptied it while I was sending". Detaching costs the
                    # one property the old cursor had for free: a concurrent
                    # _clear_buffered_frames() -- a device loss landing while
                    # this is RECONNECTING -- now empties only the fresh list,
                    # and a `finally` that re-enqueued regardless would put the
                    # dropped frames back into a buffer somebody deliberately
                    # cleared.
                    generation = self._buffer_generation
                    pending, self._buffered_frames = self._buffered_frames, []
                    position = 0
                    try:
                        while position < len(pending):
                            # Advanced only once the send has returned: a frame
                            # counted first and then failed to send is a frame
                            # nobody replays. A cursor rather than pop(0): the
                            # front-pop shifted every remaining element on
                            # every frame, and a full buffer paid that quadratic
                            # bill -- millions of moves -- on the event loop,
                            # mid-reconnect, when the loop is least affordable.
                            frame = pending[position]
                            await stream.send_bytes(
                                encode_audio_frame(
                                    channel=frame.channel,
                                    offset_ms=frame.offset_ms,
                                    pcm=frame.pcm,
                                )
                            )
                            position += 1
                            if position == len(pending):
                                pending, self._buffered_frames = self._buffered_frames, []
                                position = 0
                    finally:
                        # A replay that did not finish -- a send that failed
                        # partway through, a cancel -- puts what is left back
                        # so the next retry replays it, which is the behaviour
                        # the detached list would otherwise have thrown away.
                        # The sent prefix is dropped in one slice first, so
                        # only the frames the socket never took go back.
                        # enqueue_audio_frames keeps the buffer ordered and
                        # re-trims, so this merges correctly with anything that
                        # arrived while the replay was running. Empty on the
                        # success path, and skipped entirely once the buffer
                        # has been cleared out from under this replay -- that
                        # clear is a decision, not a gap to fill in.
                        del pending[:position]
                        if pending and self._buffer_generation == generation:
                            self.enqueue_audio_frames(pending)
                    await self._send_pending_flushes(stream)
                    self._set_state(ConnectionState.STREAMING, session=self._session)
                    # Retire before overwriting: a reader left over from the
                    # connection that just failed would otherwise keep running
                    # unreferenced against a closed stream, and _cancel_tasks
                    # only ever sees the handle stored here.
                    await self._retire_reader(self._reader_task)
                    self._reader_task = asyncio.create_task(self._listen(iterator))
                    return
                except RemoteUnauthorizedError:
                    await self._discard_recovery_stream(stream or self._recovery_stream)
                    await self._stop_capture_off_loop()
                    self._clear_buffered_frames()
                    self._notify_authentication_failure()
                    self._set_state(ConnectionState.FAILED, message="Authentication failed.")
                    return
                except RemoteCreditError:
                    await self._discard_recovery_stream(stream or self._recovery_stream)
                    await self._fail_from_remote("Session credits are unavailable.")
                    return
                except RemoteDurationError:
                    await self._discard_recovery_stream(stream or self._recovery_stream)
                    await self._fail_from_remote("Session reached its maximum duration.")
                    return
                except Exception:
                    await self._discard_recovery_stream(stream)
                    self.events.publish(
                        UiEvent(
                            type="recoverable_error",
                            message="notify.reconnect.attemptFailed",
                        )
                    )
            await self._discard_recovery_stream(self._recovery_stream)
            await self._stop_capture_off_loop()
            self._clear_buffered_frames()
            self._set_state(ConnectionState.FAILED, message="Connection could not be restored.")
        finally:
            self._recovery_active = False

    async def _end_from_remote(self) -> None:
        # Detached first, then the hop -- the order _handle_device_loss uses,
        # and for the same reason. _stop_capture_off_loop joins two daemon
        # workers on a thread, so it leaves the loop free for ~2 seconds while
        # this method is only half done. A self._stream still set across that
        # window is a socket the backend has already closed: the sender
        # dequeues a frame, sends on it, the send fails, and _forward_frame's
        # handler calls _recover -- which has no state guard at entry and would
        # open a second remote stream resuming the session that just ended.
        # Cleared beforehand, the same frame finds no stream and is dropped.
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        await self._stop_capture_off_loop()
        self._clear_buffered_frames()
        if stream is not None:
            await self._close_stream(stream)
        self._set_state(ConnectionState.STOPPED)

    async def _fail_from_remote(
        self, message: str = "The remote session could not continue."
    ) -> None:
        # Detached before the teardown hop, for the reason spelled out in
        # _end_from_remote: a stream left reachable across that window lets a
        # queued frame reconnect a session the remote has just refused.
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        await self._stop_capture_off_loop()
        self._clear_buffered_frames()
        if stream is not None:
            await self._close_stream(stream)
        self._set_state(ConnectionState.FAILED, message=message)

    def _on_pcm(self, channel: Literal["mic", "system"], pcm: bytes) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            return
        if self._muted[channel]:
            pipeline.skip(channel)
            return
        for frame in pipeline.feed(channel, pcm):
            self._produced_frames += 1
            self._schedule_forward(frame)

    def _report_audio_level(self, channel: Literal["mic", "system"], pcm: bytes) -> None:
        """Meter what is being transmitted, not what the device can hear.

        A muted channel reports a silent block of the same length rather
        than nothing at all: reporting nothing would leave the meter holding
        its last reading until it decayed, and a mute the histogram does not
        show is a mute the user cannot trust.
        """
        report = self._on_audio_level
        if report is None:
            return
        report(channel, bytes(len(pcm)) if self._muted[channel] else pcm)

    def _schedule_forward(self, frame: AudioFrame) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._enqueue_frame, frame, self._run_generation)

    def _enqueue_frame(self, frame: AudioFrame, generation: int) -> None:
        """Hand one frame to the sender, dropping the oldest when the socket is
        not keeping up.

        Dropping is the honest failure here: the alternative is an unbounded
        queue that pins ~4.8 KB per frame at 20 frames/second/channel until the
        process dies. The user is told, because silent audio loss in a
        transcription product is worse than a visible gap.

        The frame carries the run it was captured in, because a frame and the
        state it is judged against no longer meet at the same moment: the
        capture thread hands it over here, and _forward_frame does not see it
        until the sender reads it back. A frame handed over late by a finished
        run -- CaptureSession.close() joins its worker with a timeout, so the
        join can return first -- would otherwise arrive when the next run is
        streaming, look perfectly live to any state check, and be transmitted
        under a finished session's offsets.
        """
        if generation != self._run_generation or self._muted[frame.channel]:
            return
        if self._audio_queue.qsize() >= AUDIO_QUEUE_MAX_FRAMES:
            self._record_dropped_frame()
            return
        while True:
            try:
                self._audio_queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                try:
                    self._audio_queue.get_nowait()
                    self._audio_queue.task_done()
                except asyncio.QueueEmpty:
                    return
                self._record_dropped_frame()

    def _record_dropped_frame(self) -> None:
        self._dropped_frames += 1
        if not self._reported_dropping:
            self._reported_dropping = True
            self.events.publish(UiEvent(type="warning", message="notify.audio.dropping"))

    def _enqueue_flush(self, channel: Literal["mic", "system"], generation: int) -> None:
        if generation != self._run_generation or channel in self._pending_flush_channels:
            return
        self._pending_flush_channels.add(channel)
        try:
            self._audio_queue.put_nowait(_ChannelFlush(channel=channel, generation=generation))
        except asyncio.QueueFull:
            # The queue reserves two slots beyond the audio budget, one for
            # each coalesced channel marker. Retain it if that invariant ever
            # changes instead of silently losing a mute boundary.
            self._reconnect_flush_channels.add(channel)

    async def _send_audio_forever(self) -> None:
        """The only consumer of the audio queue.

        One consumer is what makes offset order a property of the code rather
        than of which task wins the drain, and it is what gives the queue a
        single place to apply backpressure.
        """
        while True:
            # The inner try only covers _forward_frame. Anything raised outside
            # it -- queue.get(), the rebinding above, task_done() disagreeing
            # with its queue -- used to kill this task for the rest of the run,
            # leaving a session that transmitted nothing while the interface
            # still said "Transmitindo" and only a "never retrieved" line in the
            # log said otherwise. A mute meeting is the worst outcome this file
            # has, so the loop survives and says so instead.
            try:
                # Bound once per iteration: _reset_audio_queue re-binds the
                # attribute between runs, and a get() and a task_done() that
                # disagree about which queue they belong to corrupt the other
                # one's counter -- or raise, on the way out of a cancel, where
                # the error is swallowed.
                queue = self._audio_queue
                item = await queue.get()
                try:
                    if isinstance(item, _ChannelFlush):
                        await self._forward_flush(item)
                    else:
                        await self._forward_frame(item)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[session] Failed to forward an audio frame")
                finally:
                    queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[session] The audio sender hit an unexpected failure")

    async def _forward_frame(self, frame: AudioFrame) -> None:
        stream = self._stream or self._recovery_stream
        if self.state is ConnectionState.RECONNECTING:
            self.enqueue_audio_frames([frame])
            return
        if self.state is not ConnectionState.STREAMING or stream is None:
            return
        try:
            await stream.send_bytes(
                encode_audio_frame(channel=frame.channel, offset_ms=frame.offset_ms, pcm=frame.pcm)
            )
            self._sent_frames += 1
        except Exception:
            self.enqueue_audio_frames([frame])
            await self._recover()

    async def _forward_flush(self, marker: _ChannelFlush) -> None:
        if marker.generation != self._run_generation:
            self._pending_flush_channels.discard(marker.channel)
            self._reconnect_flush_channels.discard(marker.channel)
            return
        stream = self._stream or self._recovery_stream
        if self.state is ConnectionState.RECONNECTING:
            self._reconnect_flush_channels.add(marker.channel)
            return
        if self.state is not ConnectionState.STREAMING or stream is None:
            self._pending_flush_channels.discard(marker.channel)
            self._reconnect_flush_channels.discard(marker.channel)
            return
        try:
            await stream.send_control({"type": "channel.flush", "channel": marker.channel})
            self._pending_flush_channels.discard(marker.channel)
            self._reconnect_flush_channels.discard(marker.channel)
        except Exception:
            self._reconnect_flush_channels.add(marker.channel)
            await self._recover()

    async def _send_pending_flushes(self, stream: RemoteStream) -> None:
        for channel in ("mic", "system"):
            if channel not in self._reconnect_flush_channels:
                continue
            await stream.send_control({"type": "channel.flush", "channel": channel})
            self._reconnect_flush_channels.discard(channel)
            self._pending_flush_channels.discard(channel)

    def _track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Hold a strong reference for the task's lifetime.

        CPython keeps only a weak reference to a running task, so a bare
        create_task can be collected mid-flight. For a frame that means silent
        audio loss; for _handle_device_loss it means a lost state transition.
        """
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _retire_reader(self, reader: asyncio.Task[None] | None) -> None:
        """Cancel a superseded transcript reader and briefly wait for it to end.

        Overwriting ``_reader_task`` while the old task still runs orphans it:
        ``_cancel_tasks`` only ever sees the handle stored there, so the old
        reader kept running -- parked on a dead socket or a backoff sleep --
        held only by the loop's weak reference, one per reconnect.

        Skips the current task: reader-driven recovery runs inside the very
        task being replaced, and that task exits on its own right after this
        returns -- the same rule ``_cancel_tasks`` follows. Bounded, so a
        reader that ignores cancellation cannot hang recovery or stop(); it is
        then handed a callback that retrieves its eventual outcome so nothing
        is reported as never retrieved.
        """
        if reader is None or reader.done() or reader is asyncio.current_task():
            return
        reader.cancel()
        done, still_pending = await asyncio.wait({reader}, timeout=READER_RETIRE_TIMEOUT_SECONDS)
        for task in done:
            if not task.cancelled():
                task.exception()
        if still_pending:
            logger.warning("[session] A superseded transcript reader ignored cancellation")
            reader.add_done_callback(lambda task: task.cancelled() or task.exception())

    async def _cancel_tasks(self) -> None:
        """Cancel everything this controller started and wait for it to finish.

        The current task is left out on purpose: these methods are reachable
        from inside one of the very tasks being cancelled, and gathering a task
        from within itself would deadlock instead of stopping.
        """
        current = asyncio.current_task()
        pending = {
            task
            for task in (self._sender_task, self._reader_task, *self._tasks)
            if task is not None and task is not current and not task.done()
        }
        self._sender_task = None
        self._reader_task = None
        self._tasks.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _reset_audio_queue(self) -> None:
        """Give every run an empty queue of its own.

        Audio queued for a run that is over would otherwise be flushed into the
        next run's socket under the previous run's offsets. Replacing the queue
        rather than draining it also keeps the controller loop-agnostic: an
        asyncio.Queue binds to the first event loop that waits on it, so a
        queue kept across runs would tie the controller to whichever loop
        opened the first one. The drop bookkeeping goes with it, so the next
        stall is reported as its own, and the run generation moves on so that
        frames captured for the run that just ended are refused on arrival.
        """
        self._audio_queue = asyncio.Queue(maxsize=AUDIO_QUEUE_MAX_FRAMES + 2)
        self._pending_flush_channels.clear()
        self._reconnect_flush_channels.clear()
        self._run_generation += 1
        self._dropped_frames = 0
        self._reported_dropping = False
        self._trimmed_frames = 0
        self._reported_trimming = False
        self._produced_frames = 0
        self._sent_frames = 0

    def _on_capture_event(self, event: CaptureEvent) -> None:
        if event.type == "device_lost":
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(
                    lambda: self._track(asyncio.create_task(self._handle_device_loss()))
                )

    async def _handle_device_loss(self) -> None:
        if self.state not in {
            ConnectionState.STARTING,
            ConnectionState.STREAMING,
            ConnectionState.RECONNECTING,
        }:
            return
        stream = self._stream or self._recovery_stream
        self._stream = None
        self._recovery_stream = None
        self._stopping = True
        await self._stop_capture_off_loop()
        self._clear_buffered_frames()
        if stream is not None:
            await self._end_and_close_stream(stream)
        self._set_state(
            ConnectionState.DEVICE_SELECTION_REQUIRED,
            message="Select a replacement capture device.",
        )
        reader = self._reader_task
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.gather(reader, return_exceptions=True)
            self._reader_task = None
        self._stopping = False

    def _stop_capture(self) -> None:
        capture = self._capture
        self._capture = None
        if capture is not None:
            capture.stop()

    async def _stop_capture_off_loop(self) -> None:
        """Detach the capture here, then close it on a worker thread.

        CaptureSession.stop() -> handle.close() joins two daemon workers with a
        one-second timeout each, plus PortAudio teardown. Run inline that is up
        to ~2 seconds of a frozen event loop -- the transcript socket, the event
        feed and the level meter all stop with it -- which is exactly what a
        device unplugged mid-meeting produces. stop() already hops for this
        reason; every other teardown path reaches it through here.

        Only the join goes off the loop. self._capture is still cleared
        synchronously, before the first await, because _open's second
        concurrent-stop check compares capture *identity* against that field
        and reads it after this returns. Overlapping with stop() is safe: the
        second caller finds self._capture already None and does nothing, and
        CaptureSession.stop() is idempotent besides.
        """
        capture = self._capture
        self._capture = None
        if capture is not None:
            await asyncio.to_thread(capture.stop)

    async def _resume_state(
        self, previous_session: SessionSummary | None, resume_code: str | None
    ) -> tuple[int, int]:
        """Return `(base_offset_ms, segment_count)` for the run about to start.

        `base_offset_ms` is where this run's captured audio must start
        counting from. Kept in-memory -- no round trip -- when this
        controller already had the session open: `_pipeline.next_offset_ms`
        already accounts for every frame captured so far this run. Opening a
        session from the library gives this controller no history for it,
        so the base has to come from what the backend already stored;
        without this, new speech would land back at offset 0, on top of what
        the meeting already has recorded there.

        `segment_count` rides along on the same round trip rather than being
        looked up separately: a library resume already has to fetch the
        session's real segment count to compute `base_offset_ms` (the
        cursor jump below needs it), so the local `SessionSummary` `_open`
        builds afterward can use that real count instead of hardcoding 0
        until new segments stream in over the live connection.

        Called only after the remote stream has confirmed which session it
        resumed (`started.uuid_code == resume_code`, checked by the caller),
        not before: spending a round trip on an identity `resume_code` has
        not yet confirmed would be wasted work, and a failure here is meant
        to unwind through the same exception handling that already covers
        `connect_stream` and the handshake read just above it.
        """
        if previous_session is not None and self._pipeline is not None:
            return self._pipeline.next_offset_ms, previous_session.segment_count
        if resume_code is None:
            return 0, 0
        remote_session = await self._remote.get_session(resume_code)
        offset_ms = await self._remote.last_segment_offset_ms(
            resume_code, remote_session.segment_count
        )
        return offset_ms, remote_session.segment_count

    def _set_pipeline(self, base_offset_ms: int) -> None:
        self._pipeline = AudioPipeline(base_offset_ms=base_offset_ms)
        self._pipeline_base_offset_ms = base_offset_ms

    async def _list_devices(self) -> list[DeviceDescriptor]:
        """Enumerate capture devices without blocking the loop.

        Prefers the lookup the API layer installs, which is
        Services.list_devices and answers from a TTL cache -- the cache exists
        precisely for this hot path, and going straight to the backend here
        defeated it. Falls back to a thread hop for a controller built without
        one, so a direct user of this class never enumerates on the loop either.
        """
        lookup = self._device_lookup
        if lookup is not None:
            return await lookup()
        return await asyncio.to_thread(self._capture_backend.list_devices)

    def _set_state(
        self,
        state: ConnectionState,
        *,
        session: SessionSummary | None = None,
        message: str | None = None,
    ) -> None:
        self.state = state
        self.events.publish(UiEvent(type="status", state=state, session=session, message=message))

    async def _sleep(self, delay: float) -> None:
        if self._clock is None:
            await asyncio.sleep(delay)
        elif callable(self._clock):
            await self._clock(delay)
        else:
            await self._clock.sleep(delay)

    @staticmethod
    async def _close_stream(stream: object) -> None:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    async def _close_open_stream(
        self, stream: RemoteStream | None, remote_period_started: bool
    ) -> None:
        if stream is None:
            return
        if remote_period_started:
            await self._end_and_close_stream(stream)
            return
        await self._close_stream(stream)

    async def _end_and_close_stream(self, stream: RemoteStream) -> None:
        """Best-effort remote finalization whose failures cannot mask the local cause."""
        try:
            await stream.send_control({"type": "session.end"})
        except Exception:
            pass
        await self._close_stream(stream)

    def _clear_buffered_frames(self) -> None:
        """Drop the reconnect buffer, and record that it was dropped.

        The counter exists for the replay in _recover, which detaches the
        buffer before sending it and therefore cannot see a clear that lands
        while it is in flight -- it would only empty the fresh list left
        behind. Bumping here is what lets that replay's `finally` tell an
        interrupted send from a buffer somebody else deliberately emptied.
        """
        self._buffered_frames.clear()
        self._buffer_generation += 1

    def _notify_authentication_failure(self) -> None:
        callback = self._on_authentication_failure
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    async def _discard_recovery_stream(self, stream: RemoteStream | None) -> None:
        if stream is None:
            return
        if self._stream is stream:
            self._stream = None
        if self._recovery_stream is stream:
            self._recovery_stream = None
        await self._close_stream(stream)

    async def _connect_terminal_stream(self) -> RemoteStream | None:
        if self._session_uuid is None:
            return None
        try:
            return await self._remote.connect_stream(
                resume_code=self._session_uuid,
                device_label=self._device_label or "",
                language_mic=self._languages.microphone,
                language_system=self._languages.system,
                title=None,
            )
        except Exception:
            return None

    @staticmethod
    def _channel(channel: str) -> Literal["mic", "system"]:
        if channel not in {"mic", "system"}:
            raise RemoteProtocolError("Remote transcript channel was invalid.")
        return channel
