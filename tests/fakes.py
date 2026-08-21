"""Deterministic in-process implementations of external desktop boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace

import pyaudiowpatch
from keyring.errors import PasswordDeleteError

from broccoli_desktop.capture import DeviceUnavailableError
from broccoli_desktop.models import (
    DeviceDescriptor,
    SegmentPage,
    SessionPage,
    SessionSummary,
    TranscriptSegment,
)
from broccoli_desktop.remote import (
    SEGMENT_PAGE_SIZE,
    RemoteEvent,
    RemoteFailure,
    RemoteProtocolError,
    RemoteUnauthorizedError,
    SessionStarted,
    TranscriptDeltaEvent,
    TranscriptSegmentEvent,
    last_page_cursor,
)

VISUAL_TEST_TOKEN = "visual-test-token"

#: Matches DESKTOP_SESSION_PAGE_SIZE on the backend, so a fake page boundary
#: falls where a real one would.
SESSION_PAGE_SIZE = 20


@dataclass
class RealisticFakeKeyring:
    """Mirrors the real Windows Credential Manager backend's strictness.

    A naive in-memory fake -- including the plain FakeKeyring this one is
    a stricter sibling of, and the hand-rolled FakeCredentials/VisualCredentials
    used at the API layer -- lets ``delete_password`` on an entry that was
    never stored succeed silently. The real backend
    (``keyring.backends.Windows.WinVaultKeyring``) does not: it raises
    ``PasswordDeleteError``, and every lenient fake in this codebase let a
    genuine 503-on-ordinary-save regression through undetected because none
    of them modelled that. This one exists so CredentialStore's delete paths
    are tested against the behaviour Windows actually has, not the behaviour
    that happens to be convenient to fake.
    """

    values: dict[tuple[str, str], str] = field(default_factory=dict)
    deleted: list[tuple[str, str]] = field(default_factory=list)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        key = (service_name, username)
        if key not in self.values:
            raise PasswordDeleteError("The specified item could not be found in the keyring.")
        del self.values[key]
        self.deleted.append(key)


class FakeRemoteClosedError(Exception):
    """Scripted remote stream closure used by lifecycle tests."""

    def __init__(self) -> None:
        super().__init__("Remote stream closed.")


@dataclass
class FakeClock:
    """Record retry delays without waiting for wall-clock time."""

    delays: list[float] = field(default_factory=list)

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)


@dataclass
class FakeLiveRemoteStream:
    """In-process stream whose events are supplied by lifecycle tests."""

    events_queue: asyncio.Queue[RemoteEvent | Exception | None] = field(
        default_factory=asyncio.Queue
    )
    frames: list[bytes] = field(default_factory=list)
    controls: list[dict[str, str]] = field(default_factory=list)
    fail_send: bool = False
    fail_control: bool = False
    fail_close: bool = False
    closed: bool = False
    lifecycle: list[str] = field(default_factory=list)
    blocked: asyncio.Event | None = None
    stagger: int = 0
    started_sends: int = 0

    def block_sends(self) -> None:
        """Stop acknowledging sends without closing.

        This is the slow-socket case: not an error, so nothing ever reaches
        RECONNECTING and the reconnect buffer never applies. The stream simply
        never drains.
        """
        self.blocked = asyncio.Event()

    def stagger_sends(self, yields: int = 32) -> None:
        """Make each send take a different number of loop turns to complete.

        A send that never yields lets even a task-per-frame producer look
        ordered, because every task then runs start to finish before the next
        one begins. Descending turns are what a real socket's drain point does
        to concurrent senders: first in is not first out.
        """
        self.stagger = yields

    async def send_bytes(self, frame: bytes) -> None:
        if self.fail_send:
            raise FakeRemoteClosedError()
        if self.blocked is not None:
            await self.blocked.wait()
        if self.stagger:
            self.started_sends += 1
            for _ in range(max(0, self.stagger - self.started_sends)):
                await asyncio.sleep(0)
        self.frames.append(frame)

    async def send_control(self, message: dict[str, str]) -> None:
        self.lifecycle.append("control")
        if self.fail_control:
            raise FakeRemoteClosedError()
        if self.closed:
            raise FakeRemoteClosedError()
        self.controls.append(message.copy())

    async def events(self) -> AsyncIterator[RemoteEvent]:
        while True:
            event = await self.events_queue.get()
            if event is None:
                return
            if isinstance(event, Exception):
                raise event
            yield event

    async def emit(self, event: RemoteEvent | Exception | None) -> None:
        await self.events_queue.put(event)

    async def close(self) -> None:
        self.lifecycle.append("close")
        if self.fail_close:
            raise FakeRemoteClosedError()
        self.closed = True
        await self.events_queue.put(None)


@dataclass
class FakeSessionRemote:
    """Offline remote that emits a fresh session.started event per connection."""

    sessions: dict[str, SessionSummary] = field(
        default_factory=lambda: {
            "session-1": SessionSummary(
                uuid_code="session-1",
                title="Existing session",
                status="live",
                started_at="2026-08-19T10:00:00Z",
                ended_at=None,
                device_label="Speakers",
                segment_count=0,
                is_live=True,
            )
        }
    )
    next_sequences: list[dict[str, int]] = field(default_factory=lambda: [{"mic": 1, "system": 1}])
    session_pages: dict[tuple[str | None, str], SessionPage] = field(default_factory=dict)
    segment_pages: dict[tuple[str, str | None], SegmentPage] = field(default_factory=dict)
    stream_event_scripts: list[list[RemoteEvent | Exception | None]] = field(default_factory=list)
    streams: list[FakeLiveRemoteStream] = field(default_factory=list)
    stream_requests: list[tuple[str | None, str]] = field(default_factory=list)
    stream_languages: list[str] = field(default_factory=list)
    fail_send_stream_indexes: set[int] = field(default_factory=set)
    fail_control_stream_indexes: set[int] = field(default_factory=set)
    fail_close_stream_indexes: set[int] = field(default_factory=set)
    unauthorized: bool = False
    verify_failure: Exception | None = None

    async def verify_token(self) -> None:
        # Injected separately from `unauthorized` on purpose: an unreachable
        # platform and a rejected credential are different answers, and the
        # window is only allowed to sign the user out for the second.
        if self.verify_failure is not None:
            raise self.verify_failure
        self._assert_authorized()

    async def list_sessions(self, cursor: str | None, query: str) -> SessionPage:
        self._assert_authorized()
        configured_page = self.session_pages.get((cursor, query))
        if configured_page is not None:
            return configured_page
        return self._page(self._matching_sessions(query), cursor)

    def _page(self, sessions: list[SessionSummary], cursor: str | None) -> SessionPage:
        """Slice by offset, the way the backend's cursor actually behaves.

        Answering every request with the whole list would let infinite scroll
        look like it works against this fake without ever asking for a page.
        """
        offset = int(cursor) if cursor else 0
        window = sessions[offset : offset + SESSION_PAGE_SIZE]
        has_more = len(sessions) > offset + SESSION_PAGE_SIZE
        return SessionPage(tuple(window), str(offset + SESSION_PAGE_SIZE) if has_more else None)

    def _matching_sessions(self, query: str) -> list[SessionSummary]:
        """Filter and order the seeded sessions the way the list endpoint does.

        Ignoring ``query`` here would let the sidebar search look like it works
        against this fake no matter what it sends. Serving them in insertion
        order would be worse: the first page would hold the oldest sessions
        while the real endpoint orders by ``-is_pinned, -pinned_at,
        -started_at``, and the client sorting its own copy would hide the
        difference right up until a page boundary mattered.
        """
        term = query.strip().casefold()
        matches = [
            session
            for session in self.sessions.values()
            if not term
            or term in session.uuid_code.casefold()
            or term in (session.device_label or "").casefold()
            or term in (session.title or "").casefold()
        ]
        return sorted(
            matches,
            key=lambda session: (
                session.is_pinned,
                session.pinned_at or "",
                session.started_at or "",
            ),
            reverse=True,
        )

    async def get_session(self, uuid_code: str) -> SessionSummary:
        self._assert_authorized()
        return self.sessions[uuid_code]

    async def list_segments(self, uuid_code: str, cursor: str | None) -> SegmentPage:
        self._assert_authorized()
        configured_page = self.segment_pages.get((uuid_code, cursor))
        if configured_page is not None:
            return configured_page
        return SegmentPage((), None)

    async def last_segment_offset_ms(self, uuid_code: str, segment_count: int) -> int:
        """Mirror HttpListeningRemote's page-jump so the arithmetic under test
        actually runs against this fake's seeded pages, not a shortcut."""
        self._assert_authorized()
        if segment_count <= 0:
            return 0
        cursor = last_page_cursor(segment_count, SEGMENT_PAGE_SIZE)
        page = await self.list_segments(uuid_code, cursor=str(cursor))
        return max((segment.ended_offset_ms for segment in page.segments), default=0)

    def seed_segments(self, uuid_code: str, *, count: int, last_ended_offset_ms: int) -> None:
        """Register a retained session's segment count and its final cursor page.

        Only the last page is populated -- a resume walk that asked for any
        earlier page would be a bug the real jump-to-the-last-page arithmetic
        is meant to prevent, and this fake should not paper over that by
        answering anyway.
        """
        existing = self.sessions.get(uuid_code)
        if existing is None:
            self.sessions[uuid_code] = SessionSummary(
                uuid_code=uuid_code,
                title="",
                status="ended",
                started_at="2026-08-19T10:00:00Z",
                ended_at="2026-08-19T11:00:00Z",
                device_label="Speakers",
                segment_count=count,
                is_live=False,
            )
        else:
            self.sessions[uuid_code] = replace(existing, segment_count=count)
        cursor = last_page_cursor(count, SEGMENT_PAGE_SIZE) if count else 0
        page_length = count - cursor
        segments = tuple(
            TranscriptSegment(
                utterance_id=f"system:{cursor + index}",
                channel="system",
                text="",
                started_offset_ms=0,
                ended_offset_ms=last_ended_offset_ms if index == page_length - 1 else 0,
            )
            for index in range(page_length)
        )
        self.segment_pages[(uuid_code, str(cursor))] = SegmentPage(segments, None)

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary:
        return await self.update_session(uuid_code, title=title)

    async def update_session(
        self, uuid_code: str, *, title: str | None = None, is_pinned: bool | None = None
    ) -> SessionSummary:
        self._assert_authorized()
        summary = self.sessions[uuid_code]
        updated = replace(
            summary,
            title=summary.title if title is None else title,
            is_pinned=summary.is_pinned if is_pinned is None else is_pinned,
            pinned_at=(
                summary.pinned_at
                if is_pinned is None
                else "2026-08-20T10:00:00Z"
                if is_pinned
                else None
            ),
        )
        self.sessions[uuid_code] = updated
        return updated

    async def delete_session(self, uuid_code: str) -> None:
        self._assert_authorized()
        del self.sessions[uuid_code]

    async def connect_stream(
        self, *, resume_code: str | None, device_label: str, language: str, title: str | None = None
    ) -> FakeLiveRemoteStream:
        self._assert_authorized()
        uuid_code = resume_code or "session-1"
        if uuid_code not in self.sessions:
            self.sessions[uuid_code] = SessionSummary(
                uuid_code=uuid_code,
                title=title or "",
                status="live",
                started_at="2026-08-19T10:00:00Z",
                ended_at=None,
                device_label=device_label,
                segment_count=0,
                is_live=True,
            )
        elif resume_code is None and title is not None:
            self.sessions[uuid_code] = replace(self.sessions[uuid_code], title=title)
        stream_index = len(self.streams)
        next_sequences = self.next_sequences[min(stream_index, len(self.next_sequences) - 1)]
        stream = FakeLiveRemoteStream(
            fail_send=len(self.streams) in self.fail_send_stream_indexes,
            fail_control=len(self.streams) in self.fail_control_stream_indexes,
            fail_close=len(self.streams) in self.fail_close_stream_indexes,
        )
        self.streams.append(stream)
        self.stream_requests.append((resume_code, device_label))
        self.stream_languages.append(language)
        await stream.emit(SessionStarted(uuid_code, next_sequences, 14_400))
        if stream_index < len(self.stream_event_scripts):
            for event in self.stream_event_scripts[stream_index]:
                await stream.emit(event)
        return stream

    async def emit_delta(self, channel: str, utterance_id: str, text: str) -> None:
        await self.streams[-1].emit(TranscriptDeltaEvent(channel, utterance_id, text, 500))

    async def emit_segment(
        self,
        channel: str,
        utterance_id: str,
        text: str,
        started_offset_ms: int,
        ended_offset_ms: int,
    ) -> None:
        await self.streams[-1].emit(
            TranscriptSegmentEvent(channel, utterance_id, text, started_offset_ms, ended_offset_ms)
        )

    async def emit_failure(self) -> None:
        await self.streams[-1].emit(FakeRemoteClosedError())

    async def emit_credit_denied(self) -> None:
        await self.streams[-1].emit(RemoteFailure())

    async def emit_ended(self) -> None:
        from broccoli_desktop.remote import SessionEnded

        await self.streams[-1].emit(SessionEnded())

    def revoke_token(self) -> None:
        self.unauthorized = True

    def _assert_authorized(self) -> None:
        if self.unauthorized:
            raise RemoteUnauthorizedError


@dataclass
class FakeRemoteStream:
    """A scripted stream that never opens a socket or contacts a network endpoint."""

    scripted_events: list[RemoteEvent | Exception] = field(default_factory=list)
    frames: list[bytes] = field(default_factory=list)
    controls: list[dict[str, str]] = field(default_factory=list)
    closed: bool = False

    async def send_bytes(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def send_control(self, message: dict[str, str]) -> None:
        self.controls.append(message.copy())

    async def events(self) -> AsyncIterator[RemoteEvent]:
        while self.scripted_events:
            event = self.scripted_events.pop(0)
            if isinstance(event, Exception):
                raise event
            yield event

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeListeningRemote:
    """Preloaded typed Listening data for offline unit and UI tests."""

    session_pages: dict[tuple[str | None, str], SessionPage] = field(default_factory=dict)
    sessions: dict[str, SessionSummary] = field(default_factory=dict)
    segment_pages: dict[tuple[str, str | None], SegmentPage] = field(default_factory=dict)
    stream: FakeRemoteStream = field(default_factory=FakeRemoteStream)
    unauthorized: bool = False
    stream_requests: list[tuple[str | None, str]] = field(default_factory=list)

    async def verify_token(self) -> None:
        self._assert_authorized()

    async def list_sessions(self, cursor: str | None, query: str) -> SessionPage:
        self._assert_authorized()
        return self.session_pages.get((cursor, query), SessionPage((), None))

    async def get_session(self, uuid_code: str) -> SessionSummary:
        self._assert_authorized()
        try:
            return self.sessions[uuid_code]
        except KeyError:
            raise RemoteProtocolError("Fake session was not configured.") from None

    async def list_segments(self, uuid_code: str, cursor: str | None) -> SegmentPage:
        self._assert_authorized()
        return self.segment_pages.get((uuid_code, cursor), SegmentPage((), None))

    async def last_segment_offset_ms(self, uuid_code: str, segment_count: int) -> int:
        self._assert_authorized()
        if segment_count <= 0:
            return 0
        cursor = last_page_cursor(segment_count, SEGMENT_PAGE_SIZE)
        page = await self.list_segments(uuid_code, cursor=str(cursor))
        return max((segment.ended_offset_ms for segment in page.segments), default=0)

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary:
        return await self.update_session(uuid_code, title=title)

    async def update_session(
        self, uuid_code: str, *, title: str | None = None, is_pinned: bool | None = None
    ) -> SessionSummary:
        session = await self.get_session(uuid_code)
        updated = replace(
            session,
            title=session.title if title is None else title,
            is_pinned=session.is_pinned if is_pinned is None else is_pinned,
            pinned_at=(
                session.pinned_at
                if is_pinned is None
                else "2026-08-20T10:00:00Z"
                if is_pinned
                else None
            ),
        )
        self.sessions[uuid_code] = updated
        return updated

    async def delete_session(self, uuid_code: str) -> None:
        self._assert_authorized()
        try:
            del self.sessions[uuid_code]
        except KeyError:
            raise RemoteProtocolError("Fake session was not configured.") from None

    async def connect_stream(
        self, *, resume_code: str | None, device_label: str, language: str, title: str | None = None
    ) -> FakeRemoteStream:
        del language, title
        self._assert_authorized()
        self.stream_requests.append((resume_code, device_label))
        return self.stream

    def revoke_token(self) -> None:
        self.unauthorized = True

    def _assert_authorized(self) -> None:
        if self.unauthorized:
            raise RemoteUnauthorizedError


async def settle() -> None:
    """Let the loop, and the one worker thread behind it, finish what is pending.

    Two turns used to be enough because capture teardown ran inline. It no
    longer does -- _stop_capture_off_loop hands the blocking join to a worker
    thread -- and yielding to the loop does not wait for a thread, so the same
    two turns would sample the state mid-teardown.

    The to_thread call in the middle is the barrier, not a delay: tests/
    conftest.py pins the loop's default executor to a single worker, so this
    no-op cannot start until whatever the controller handed over has finished.
    The turns on either side are what let the controller reach its hand-off in
    the first place, and then let its continuation run once the thread returns.
    """
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.to_thread(lambda: None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def unauthorized_remote() -> FakeListeningRemote:
    """Return an offline fake that rejects login attempts."""
    return FakeListeningRemote(unauthorized=True)


def revoked_remote() -> FakeListeningRemote:
    """Return an offline fake whose token is revoked after setup."""
    remote = FakeListeningRemote()
    remote.revoke_token()
    return remote


def resumed_session_started() -> SessionStarted:
    """Return a resumed handshake with separate channel sequences."""
    return SessionStarted(
        uuid_code="session-1",
        next_sequence_by_channel={"mic": 12, "system": 8},
        max_duration_s=14_400,
    )


def delta_final_pair() -> tuple[TranscriptDeltaEvent, TranscriptSegmentEvent]:
    """Return a delta and final segment that share one utterance identifier."""
    return (
        TranscriptDeltaEvent(
            channel="system",
            utterance_id="utterance-1",
            text="we should",
            started_offset_ms=500,
        ),
        TranscriptSegmentEvent(
            channel="system",
            utterance_id="utterance-1",
            text="we should ship",
            started_offset_ms=500,
            ended_offset_ms=1_000,
        ),
    )


def remote_close(events: Sequence[RemoteEvent] = ()) -> FakeRemoteStream:
    """Return a stream that yields events before an in-process remote closure."""
    return FakeRemoteStream([*events, FakeRemoteClosedError()])


@dataclass
class VisualTestRemoteFactory:
    """Return the one accepted browser-test remote without network access."""

    remote: FakeSessionRemote = field(default_factory=lambda: visual_test_remote())

    def __call__(self, token: str) -> FakeSessionRemote:
        if token != VISUAL_TEST_TOKEN:
            return FakeSessionRemote(unauthorized=True)
        return self.remote


def visual_history() -> dict[str, SessionSummary]:
    """Enough retained sessions to need three pages in the browser check.

    Three, not two: with a single page-boundary the sentinel is already in view
    when the first page lands, the second page loads before anyone scrolls, and
    the check cannot tell automatic loading from having fetched everything.

    The timestamps deliberately mix the two shapes the backend emits -- with and
    without fractional seconds -- because comparing them as text is what used to
    put the oldest session at the top of the list.

    Indices 0-2 land on 2026-08-18, a day earlier than the other 42; index 2 is
    pinned, leaving indices 0-1 as ordinary (unpinned) sessions on that same
    earlier day. That combination -- a pin from an older day, with more of that
    day still sitting unpinned further down -- is what a real account hits
    constantly, and what used to make the day heading for 2026-08-18 render
    twice, non-adjacently, once for the pin and again lower down among the
    unpinned rows sharing its date. visual-check.ps1 asserts against this
    directly: the day headings it finds must be unique and strictly descending.
    """
    history: dict[str, SessionSummary] = {}
    for index in range(SESSION_PAGE_SIZE * 2 + 5):
        minute = f"{index:02d}"
        fraction = ".123456" if index % 2 else ""
        uuid_code = f"history-{index:02d}"
        day = "18" if index < 3 else "19"
        is_pinned = index == 2
        history[uuid_code] = SessionSummary(
            uuid_code=uuid_code,
            title=f"Reunião arquivada {index:02d}",
            status="ended",
            started_at=f"2026-08-{day}T10:{minute}:00{fraction}Z",
            ended_at=f"2026-08-{day}T11:{minute}:00Z",
            device_label="Speakers",
            segment_count=0,
            is_live=False,
            is_pinned=is_pinned,
            pinned_at="2026-08-20T09:00:00Z" if is_pinned else None,
        )
    return history


def visual_test_remote() -> FakeSessionRemote:
    """Seed a paginated history, a fresh fake capture and one final segment."""
    delta, segment = delta_final_pair()
    return FakeSessionRemote(
        sessions=visual_history(),
        next_sequences=[{"mic": 1, "system": 1}],
        stream_event_scripts=[[delta, segment]],
    )


def visual_test_remote_factory() -> VisualTestRemoteFactory:
    """Build the deterministic token gate used by the browser-only fake server."""
    return VisualTestRemoteFactory()


@dataclass
class FakeCaptureHandle:
    """In-memory capture source with observable cleanup and loss delivery."""

    device_id: str
    closed_sources: set[str]
    on_pcm: Callable[[bytes], None] | None = None
    drained: bool = False
    closed: bool = False
    pending_error: Exception | None = None
    _on_error: Callable[[Exception], None] | None = None

    def close(self) -> None:
        self.closed = True
        self.closed_sources.add(self.device_id)

    def drain(self) -> None:
        self.drained = True

    def set_error_handler(self, on_error: Callable[[Exception], None]) -> None:
        self._on_error = on_error
        if self.pending_error is not None:
            on_error(self.pending_error)

    def lose_device(self) -> None:
        if self._on_error is None:
            raise RuntimeError("No device-loss handler was installed.")
        self._on_error(DeviceUnavailableError(self.device_id))

    def emit(self, pcm: bytes) -> None:
        if self.on_pcm is None:
            raise RuntimeError("No PCM callback was installed.")
        self.on_pcm(pcm)


@dataclass
class FakeCaptureBackend:
    """Offline capture backend that can synchronously deliver a scripted block."""

    devices: list[DeviceDescriptor] = field(
        default_factory=lambda: [
            DeviceDescriptor("mic-1", "Microphone One", "mic"),
            DeviceDescriptor("system-1", "Speakers", "system"),
        ]
    )
    fail_opening: str | None = None
    callback_pcm: bytes | None = None
    callback_device_id: str | None = None
    error_before_handler_id: str | None = None
    require_listed_devices: bool = False
    closed_sources: set[str] = field(default_factory=set)
    handles: dict[str, FakeCaptureHandle] = field(default_factory=dict)
    list_devices_calls: int = 0

    def list_devices(self) -> list[DeviceDescriptor]:
        self.list_devices_calls += 1
        return list(self.devices)

    def open_microphone(self, device_id: str, on_pcm: Callable[[bytes], None]) -> FakeCaptureHandle:
        return self._open(device_id, on_pcm)

    def open_loopback(self, device_id: str, on_pcm: Callable[[bytes], None]) -> FakeCaptureHandle:
        return self._open(device_id, on_pcm)

    def _open(self, device_id: str, on_pcm: Callable[[bytes], None]) -> FakeCaptureHandle:
        if self.fail_opening == device_id:
            raise OSError("The selected device is unavailable.")
        if self.require_listed_devices and device_id not in {
            device.device_id for device in self.devices
        }:
            raise DeviceUnavailableError(device_id)
        pending_error = (
            DeviceUnavailableError(device_id) if self.error_before_handler_id == device_id else None
        )
        handle = FakeCaptureHandle(
            device_id,
            self.closed_sources,
            on_pcm=on_pcm,
            pending_error=pending_error,
        )
        self.handles[device_id] = handle
        if self.callback_pcm is not None and self.callback_device_id == device_id:
            try:
                on_pcm(self.callback_pcm)
            except Exception:
                handle.close()
                raise
        return handle


@dataclass
class FakePyAudioStream:
    """Minimal stream double that invokes the configured PortAudio callback."""

    callback: Callable[[bytes, int, object, int], tuple[None, int]]
    closed: bool = False
    stopped: bool = False

    def emit(self, pcm: bytes, status_flags: int = 0) -> None:
        self.callback(pcm, len(pcm) // 2, {}, status_flags)

    def raise_input_overflow_then_device_removed(self) -> None:
        """Drive the real capture.py status-flag branch the way WASAPI does
        mid-stream: a transient input-overflow flag, immediately followed by
        the flags PortAudio keeps reporting once the endpoint itself is gone.

        PortAudio's callback has no dedicated "device removed" bit -- both a
        benign xrun and an actual disappearance arrive through the same
        status_flags word, which is exactly what the production callback at
        capture.py inspects with `if status_flags:`. The second call reuses
        real overflow/underflow flags to model that repeated-xrun signature.
        """
        self.callback(b"", 0, {}, pyaudiowpatch.paInputOverflow)
        self.callback(b"", 0, {}, pyaudiowpatch.paInputOverflow | pyaudiowpatch.paInputUnderflow)

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


@dataclass
class FakePyAudio:
    """PyAudioWPatch-shaped fake that neither probes nor opens hardware."""

    device_infos: list[dict[str, object]] = field(
        default_factory=lambda: [
            {
                "index": 1,
                "name": "Microphone One",
                "maxInputChannels": 1,
                "maxOutputChannels": 0,
                "hostApi": 0,
                "isLoopbackDevice": False,
            },
            {
                "index": 2,
                "name": "Speakers",
                "maxInputChannels": 0,
                "maxOutputChannels": 2,
                "hostApi": 0,
                "isLoopbackDevice": False,
            },
        ]
    )
    loopback_infos: list[dict[str, object]] = field(
        default_factory=lambda: [
            {
                "index": 3,
                "name": "Speakers (loopback)",
                "maxInputChannels": 2,
                "maxOutputChannels": 0,
                "hostApi": 0,
                "isLoopbackDevice": True,
            }
        ]
    )
    open_calls: list[dict[str, object]] = field(default_factory=list)
    streams: list[FakePyAudioStream] = field(default_factory=list)

    def get_device_info_generator(self):
        yield from self.device_infos

    def get_loopback_device_info_generator(self):
        yield from self.loopback_infos

    def get_wasapi_loopback_analogue_by_index(self, index: int) -> dict[str, object]:
        if index != 2:
            raise LookupError("No loopback device was found.")
        return self.loopback_infos[0]

    def open(self, **kwargs: object) -> FakePyAudioStream:
        self.open_calls.append(kwargs)
        stream = FakePyAudioStream(kwargs["stream_callback"])
        self.streams.append(stream)
        return stream
