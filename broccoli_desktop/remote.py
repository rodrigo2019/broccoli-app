"""Typed adapter for the external Listening HTTP and WebSocket contract."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets

from broccoli_desktop.models import SegmentPage, SessionPage, SessionSummary, TranscriptSegment

SESSION_LIST_PATH = "/api/listening/desktop/sessions/"
STREAM_PATH = "/ws/listening/"


class RemoteError(Exception):
    """Base error for the external Listening boundary."""


class RemoteUnauthorizedError(RemoteError):
    """The remote token was rejected without exposing credential material."""

    def __init__(self) -> None:
        super().__init__("Remote authentication failed.")


class RemoteRequestError(RemoteError):
    """A remote request failed without exposing its response."""

    def __init__(self) -> None:
        super().__init__("Remote request failed.")


class RemoteProtocolError(RemoteError):
    """The remote contract returned an unsupported or invalid event."""


@dataclass(frozen=True)
class SessionStarted:
    uuid_code: str
    next_seq: int
    next_offset_ms: int
    max_duration_s: int


@dataclass(frozen=True)
class TranscriptDeltaEvent:
    channel: str
    utterance_id: str
    text: str
    started_offset_ms: int


@dataclass(frozen=True)
class TranscriptSegmentEvent:
    channel: str
    utterance_id: str
    text: str
    started_offset_ms: int
    ended_offset_ms: int


@dataclass(frozen=True)
class CreditWarning:
    pass


@dataclass(frozen=True)
class SessionEnded:
    pass


@dataclass(frozen=True)
class RemoteFailure:
    pass


@dataclass(frozen=True)
class Pong:
    pass


type RemoteEvent = (
    SessionStarted
    | TranscriptDeltaEvent
    | TranscriptSegmentEvent
    | CreditWarning
    | SessionEnded
    | RemoteFailure
    | Pong
)


class RemoteStream(Protocol):
    async def send_bytes(self, frame: bytes) -> None: ...

    async def send_control(self, message: dict[str, str]) -> None: ...

    def events(self) -> AsyncIterator[RemoteEvent]: ...

    async def close(self) -> None: ...


class ListeningRemote(Protocol):
    async def verify_token(self) -> SessionPage: ...

    async def list_sessions(self, cursor: str | None, query: str) -> SessionPage: ...

    async def get_session(self, uuid_code: str) -> SessionSummary: ...

    async def list_segments(self, uuid_code: str, cursor: str | None) -> SegmentPage: ...

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary: ...

    async def connect_stream(
        self, *, resume_code: str | None, device_label: str
    ) -> RemoteStream: ...


class WebSocketConnection(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


type SocketFactory = Callable[..., Awaitable[WebSocketConnection]]


class HttpListeningRemote:
    """Map the external Listening contract into immutable desktop models."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._socket_factory = socket_factory or websockets.connect

    async def verify_token(self) -> SessionPage:
        return await self.list_sessions(cursor=None, query="")

    async def list_sessions(self, cursor: str | None, query: str) -> SessionPage:
        payload = await self._request(
            "GET", SESSION_LIST_PATH, params={"cursor": cursor, "q": query}
        )
        return _session_page(payload)

    async def get_session(self, uuid_code: str) -> SessionSummary:
        payload = await self._request("GET", f"{SESSION_LIST_PATH}{uuid_code}/")
        return _session_summary(payload)

    async def list_segments(self, uuid_code: str, cursor: str | None) -> SegmentPage:
        payload = await self._request(
            "GET", f"{SESSION_LIST_PATH}{uuid_code}/segments/", params={"cursor": cursor}
        )
        return _segment_page(payload)

    async def update_title(self, uuid_code: str, title: str) -> SessionSummary:
        payload = await self._request(
            "PATCH", f"{SESSION_LIST_PATH}{uuid_code}/", json={"title": title}
        )
        return _session_summary(payload)

    async def connect_stream(self, *, resume_code: str | None, device_label: str) -> RemoteStream:
        try:
            socket = await self._socket_factory(
                _stream_url(self._base_url),
                additional_headers={"Authorization": _authorization_header(self._token)},
            )
        except Exception:
            raise RemoteRequestError from None
        stream = _WebSocketRemoteStream(socket)
        await stream.send_control(
            {
                "type": "session.start",
                "resume_code": resume_code or "",
                "device_label": device_label,
            }
        )
        return stream

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | None] | None = None,
        json: dict[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": _authorization_header(self._token)},
                transport=self._transport,
            ) as client:
                response = await client.request(method, path, params=params, json=json)
        except httpx.HTTPError:
            raise RemoteRequestError from None
        if response.status_code in {401, 403}:
            raise RemoteUnauthorizedError
        if response.is_error:
            raise RemoteRequestError
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            raise RemoteProtocolError("Remote response was invalid.") from None
        if not isinstance(payload, dict):
            raise RemoteProtocolError("Remote response was invalid.")
        return payload


class _WebSocketRemoteStream:
    def __init__(self, socket: WebSocketConnection) -> None:
        self._socket = socket

    async def send_bytes(self, frame: bytes) -> None:
        await self._socket.send(frame)

    async def send_control(self, message: dict[str, str]) -> None:
        await self._socket.send(json.dumps(message))

    async def events(self) -> AsyncIterator[RemoteEvent]:
        while True:
            try:
                message = await self._socket.recv()
            except StopAsyncIteration:
                return
            except Exception:
                raise RemoteRequestError from None
            yield _remote_event(message)

    async def close(self) -> None:
        await self._socket.close()


def _authorization_header(token: str) -> str:
    return f"Token {token}"


def _stream_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme)
    if scheme is None:
        raise ValueError("Listening server URL must use HTTP or HTTPS.")
    return urlunsplit((scheme, parts.netloc, STREAM_PATH, "", ""))


def _session_page(payload: Mapping[str, object]) -> SessionPage:
    sessions = _required_list(payload, "sessions")
    return SessionPage(
        sessions=tuple(_session_summary(item) for item in sessions),
        next_cursor=_optional_string(payload, "next_cursor"),
    )


def _segment_page(payload: Mapping[str, object]) -> SegmentPage:
    segments = _required_list(payload, "segments")
    return SegmentPage(
        segments=tuple(_transcript_segment(item) for item in segments),
        next_cursor=_optional_string(payload, "next_cursor"),
    )


def _session_summary(payload: object) -> SessionSummary:
    values = _mapping(payload)
    return SessionSummary(
        uuid_code=_required_string(values, "uuid_code"),
        title=_required_string(values, "title"),
        status=_required_string(values, "status"),
        started_at=_optional_string(values, "started_at"),
        ended_at=_optional_string(values, "ended_at"),
        device_label=_required_string(values, "device_label"),
        segment_count=_required_integer(values, "segment_count"),
        is_live=_required_boolean(values, "is_live"),
    )


def _transcript_segment(payload: object) -> TranscriptSegment:
    values = _mapping(payload)
    return TranscriptSegment(
        utterance_id=_required_string(values, "utterance_id"),
        channel=_channel(values),
        text=_required_string(values, "text"),
        started_offset_ms=_required_integer(values, "started_offset_ms"),
        ended_offset_ms=_required_integer(values, "ended_offset_ms"),
    )


def _remote_event(message: str | bytes) -> RemoteEvent:
    if not isinstance(message, str):
        raise RemoteProtocolError("Remote event was invalid.")
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        raise RemoteProtocolError("Remote event was invalid.") from None
    values = _mapping(payload)
    event_type = _required_string(values, "type")
    if event_type == "session.started":
        return SessionStarted(
            uuid_code=_required_string(values, "uuid_code"),
            next_seq=_required_integer(values, "next_seq"),
            next_offset_ms=_required_integer(values, "next_offset_ms"),
            max_duration_s=_required_integer(values, "max_duration_s"),
        )
    if event_type == "transcript.delta":
        return TranscriptDeltaEvent(
            channel=_channel(values),
            utterance_id=_required_string(values, "utterance_id"),
            text=_required_string(values, "text"),
            started_offset_ms=_required_integer(values, "started_offset_ms"),
        )
    if event_type == "transcript.segment":
        return TranscriptSegmentEvent(
            channel=_channel(values),
            utterance_id=_required_string(values, "utterance_id"),
            text=_required_string(values, "text"),
            started_offset_ms=_required_integer(values, "started_offset_ms"),
            ended_offset_ms=_required_integer(values, "ended_offset_ms"),
        )
    if event_type == "credit.warning":
        return CreditWarning()
    if event_type == "session.ended":
        return SessionEnded()
    if event_type == "error":
        return RemoteFailure()
    if event_type == "pong":
        return Pong()
    raise RemoteProtocolError(f"Unexpected remote event type: {event_type}")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


def _required_list(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


def _required_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


def _required_boolean(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


def _channel(payload: Mapping[str, object]) -> str:
    channel = _required_string(payload, "channel")
    if channel not in {"mic", "system"}:
        raise RemoteProtocolError("Remote payload was invalid.")
    return channel
