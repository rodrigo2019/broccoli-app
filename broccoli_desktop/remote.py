"""Typed adapter for the declared Listening HTTP and WebSocket contract."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import websockets

AUTH_ME_PATH = "/api/auth/me/"


class RemoteError(Exception):
    """Base error for the external Listening boundary."""


class RemoteUnauthorizedError(RemoteError):
    """The remote token was rejected without exposing credential material."""

    def __init__(self) -> None:
        super().__init__("Remote authentication failed.")


class RemoteCreditError(RemoteError):
    """The remote closed because the account has no Listening credit."""

    def __init__(self) -> None:
        super().__init__("Remote session credits are unavailable.")


class RemoteDurationError(RemoteError):
    """The remote closed because the Listening session reached its limit."""

    def __init__(self) -> None:
        super().__init__("Remote session reached its maximum duration.")


class RemoteRequestError(RemoteError):
    """A remote request failed without exposing its response."""

    def __init__(self) -> None:
        super().__init__("Remote request failed.")


class RemoteProtocolError(RemoteError):
    """The remote contract returned an unsupported or invalid event."""


@dataclass(frozen=True)
class SessionStarted:
    uuid_code: str
    next_sequence_by_channel: Mapping[str, int]
    max_duration_s: int


@dataclass(frozen=True)
class TranscriptDeltaEvent:
    """Legacy fake event retained while the local UI transition is completed."""

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
    async def verify_token(self) -> None: ...

    async def connect_stream(
        self, *, resume_code: str | None, device_label: str, language: str
    ) -> RemoteStream: ...


class WebSocketConnection(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


type SocketFactory = Callable[..., Awaitable[WebSocketConnection]]


class HttpListeningRemote:
    """Map the declared Listening contract into safe desktop events."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        websocket_path: str,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._websocket_path = websocket_path
        self._socket_factory = socket_factory or websockets.connect

    async def verify_token(self) -> None:
        await self._request_json("GET", AUTH_ME_PATH)

    async def connect_stream(
        self, *, resume_code: str | None, device_label: str, language: str
    ) -> RemoteStream:
        try:
            socket = await self._socket_factory(
                _stream_url(
                    self._base_url,
                    self._websocket_path,
                    resume_code=resume_code,
                    device_label=device_label,
                    language=language,
                ),
                additional_headers={"Authorization": _authorization_header(self._token)},
            )
        except Exception as error:
            if _handshake_status_code(error) in {401, 403}:
                raise RemoteUnauthorizedError from None
            raise RemoteRequestError from None
        return _WebSocketRemoteStream(socket)

    async def _request_json(self, method: str, path: str) -> object:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": _authorization_header(self._token)},
                transport=self._transport,
            ) as client:
                response = await client.request(method, path)
        except httpx.HTTPError:
            raise RemoteRequestError from None
        if response.status_code in {401, 403}:
            raise RemoteUnauthorizedError
        if response.is_error:
            raise RemoteRequestError
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            raise RemoteProtocolError("Remote response was invalid.") from None


class _WebSocketRemoteStream:
    def __init__(self, socket: WebSocketConnection) -> None:
        self._socket = socket
        self._next_sequence_by_channel: dict[str, int] | None = None

    async def send_bytes(self, frame: bytes) -> None:
        try:
            await self._socket.send(frame)
        except Exception as error:
            _raise_safe_remote_error(error)

    async def send_control(self, message: dict[str, str]) -> None:
        try:
            await self._socket.send(json.dumps(message))
        except Exception as error:
            _raise_safe_remote_error(error)

    async def events(self) -> AsyncIterator[RemoteEvent]:
        while True:
            try:
                message = await self._socket.recv()
            except StopAsyncIteration:
                return
            except Exception as error:
                _raise_safe_remote_error(error)
            yield self._event(message)

    async def close(self) -> None:
        try:
            await self._socket.close()
        except Exception as error:
            _raise_safe_remote_error(error)

    def _event(self, message: str | bytes) -> RemoteEvent:
        values = _event_values(message)
        event_type = _required_string(values, "type")
        if event_type == "session.started":
            sequences = _next_sequences(values)
            self._next_sequence_by_channel = dict(sequences)
            return SessionStarted(
                uuid_code=_required_string(values, "uuid_code"),
                next_sequence_by_channel=MappingProxyType(sequences),
                max_duration_s=_required_integer(values, "max_duration_s"),
            )
        if event_type == "transcript.segment":
            channel = _channel(values)
            sequence = self._sequence_for(channel)
            return TranscriptSegmentEvent(
                channel=channel,
                utterance_id=f"{channel}:{sequence}",
                text=_required_string(values, "text"),
                started_offset_ms=_required_integer(values, "started_offset_ms"),
                ended_offset_ms=_required_integer(values, "ended_offset_ms"),
            )
        if event_type == "transcript.delta":
            channel = _channel(values)
            sequence = self._current_sequence_for(channel)
            return TranscriptDeltaEvent(
                channel=channel,
                utterance_id=f"{channel}:{sequence}",
                text=_required_string(values, "text"),
                started_offset_ms=_required_integer(values, "started_offset_ms"),
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

    def _sequence_for(self, channel: str) -> int:
        sequence = self._current_sequence_for(channel)
        assert self._next_sequence_by_channel is not None  # noqa: S101 - guarded above
        self._next_sequence_by_channel[channel] = sequence + 1
        return sequence

    def _current_sequence_for(self, channel: str) -> int:
        sequences = self._next_sequence_by_channel
        if sequences is None or channel not in sequences:
            raise RemoteProtocolError("Remote segment arrived before a session started.")
        return sequences[channel]


def _authorization_header(token: str) -> str:
    return f"Token {token}"


def _stream_url(
    base_url: str,
    websocket_path: str,
    *,
    resume_code: str | None,
    device_label: str,
    language: str,
) -> str:
    parts = urlsplit(base_url)
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme)
    if scheme is None:
        raise ValueError("Listening server URL must use HTTP or HTTPS.")
    query = urlencode(
        {
            key: value
            for key, value in {
                "resume": resume_code,
                "device": device_label,
                "language": language,
            }.items()
            if value
        }
    )
    return urlunsplit((scheme, parts.netloc, websocket_path, query, ""))


def _handshake_status_code(error: Exception) -> int | None:
    """Read only an exposed handshake status, never an error response payload."""
    response = getattr(error, "response", None)
    for source in (error, response):
        status_code = getattr(source, "status_code", None)
        if isinstance(status_code, int):
            return status_code
    return None


def _raise_safe_remote_error(error: Exception) -> None:
    code = _close_code(error)
    if code == 4401:
        raise RemoteUnauthorizedError from None
    if code == 4402:
        raise RemoteCreditError from None
    if code == 4403:
        raise RemoteDurationError from None
    raise RemoteRequestError from None


def _close_code(error: Exception) -> int | None:
    direct = getattr(error, "code", None)
    if isinstance(direct, int):
        return direct
    for close in (getattr(error, "rcvd", None), getattr(error, "sent", None)):
        code = getattr(close, "code", None)
        if isinstance(code, int):
            return code
    return None


def _event_values(message: str | bytes) -> Mapping[str, object]:
    if not isinstance(message, str):
        raise RemoteProtocolError("Remote event was invalid.")
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        raise RemoteProtocolError("Remote event was invalid.") from None
    if not isinstance(payload, dict):
        raise RemoteProtocolError("Remote payload was invalid.")
    return payload


def _next_sequences(payload: Mapping[str, object]) -> dict[str, int]:
    value = payload.get("next_seq")
    if not isinstance(value, dict):
        raise RemoteProtocolError("Remote payload was invalid.")
    sequences = {channel: _required_integer(value, channel) for channel in ("mic", "system")}
    return sequences


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


def _required_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


def _channel(payload: Mapping[str, object]) -> str:
    channel = _required_string(payload, "channel")
    if channel not in {"mic", "system"}:
        raise RemoteProtocolError("Remote payload was invalid.")
    return channel
