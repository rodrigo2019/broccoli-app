"""Deterministic in-process implementations of the external Listening boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace

from broccoli_desktop.models import SegmentPage, SessionPage, SessionSummary
from broccoli_desktop.remote import (
    RemoteEvent,
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
