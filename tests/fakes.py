"""Deterministic in-process implementations of external desktop boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace

from broccoli_desktop.capture import DeviceUnavailableError
from broccoli_desktop.models import DeviceDescriptor, SegmentPage, SessionPage, SessionSummary
from broccoli_desktop.remote import (
    RemoteEvent,
    RemoteFailure,
    RemoteProtocolError,
    RemoteUnauthorizedError,
    SessionStarted,
    TranscriptDeltaEvent,
    TranscriptSegmentEvent,
)


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
    closed: bool = False

    async def send_bytes(self, frame: bytes) -> None:
        if self.fail_send:
            raise FakeRemoteClosedError()
        self.frames.append(frame)

    async def send_control(self, message: dict[str, str]) -> None:
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
    next_offsets: list[int] = field(default_factory=lambda: [0])
    streams: list[FakeLiveRemoteStream] = field(default_factory=list)
    stream_requests: list[tuple[str | None, str]] = field(default_factory=list)
    fail_send_stream_indexes: set[int] = field(default_factory=set)
    unauthorized: bool = False

    async def verify_token(self) -> SessionPage:
        self._assert_authorized()
        return SessionPage(tuple(self.sessions.values()), None)

    async def list_sessions(self, cursor: str | None, query: str) -> SessionPage:
        self._assert_authorized()
        return SessionPage(tuple(self.sessions.values()), None)

    async def get_session(self, uuid_code: str) -> SessionSummary:
        self._assert_authorized()
        return self.sessions[uuid_code]

    async def list_segments(self, uuid_code: str, cursor: str | None) -> SegmentPage:
        self._assert_authorized()
        return SegmentPage((), None)

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary:
        self._assert_authorized()
        summary = self.sessions[uuid_code]
        updated = replace(summary, title=title)
        self.sessions[uuid_code] = updated
        return updated

    async def connect_stream(
        self, *, resume_code: str | None, device_label: str
    ) -> FakeLiveRemoteStream:
        self._assert_authorized()
        uuid_code = resume_code or "session-1"
        if uuid_code not in self.sessions:
            self.sessions[uuid_code] = SessionSummary(
                uuid_code=uuid_code,
                title="",
                status="live",
                started_at="2026-08-19T10:00:00Z",
                ended_at=None,
                device_label=device_label,
                segment_count=0,
                is_live=True,
            )
        offset = self.next_offsets[min(len(self.streams), len(self.next_offsets) - 1)]
        stream = FakeLiveRemoteStream(fail_send=len(self.streams) in self.fail_send_stream_indexes)
        self.streams.append(stream)
        self.stream_requests.append((resume_code, device_label))
        await stream.emit(SessionStarted(uuid_code, 1, offset, 14_400))
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

    async def verify_token(self) -> SessionPage:
        self._assert_authorized()
        return await self.list_sessions(cursor=None, query="")

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

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary:
        session = await self.get_session(uuid_code)
        updated = replace(session, title=title)
        self.sessions[uuid_code] = updated
        return updated

    async def connect_stream(
        self, *, resume_code: str | None, device_label: str
    ) -> FakeRemoteStream:
        self._assert_authorized()
        self.stream_requests.append((resume_code, device_label))
        return self.stream

    def revoke_token(self) -> None:
        self.unauthorized = True

    def _assert_authorized(self) -> None:
        if self.unauthorized:
            raise RemoteUnauthorizedError


def unauthorized_remote() -> FakeListeningRemote:
    """Return an offline fake that rejects login attempts."""
    return FakeListeningRemote(unauthorized=True)


def revoked_remote() -> FakeListeningRemote:
    """Return an offline fake whose token is revoked after setup."""
    remote = FakeListeningRemote()
    remote.revoke_token()
    return remote


def resumed_session_started() -> SessionStarted:
    """Return the required nonzero resume offset event."""
    return SessionStarted(
        uuid_code="session-1",
        next_seq=12,
        next_offset_ms=45_000,
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

    def list_devices(self) -> list[DeviceDescriptor]:
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
