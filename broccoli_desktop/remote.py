"""Typed adapter for the declared Listening HTTP and WebSocket contract."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
import websockets

from broccoli_desktop.models import SegmentPage, SessionPage, SessionSummary, TranscriptSegment

AUTH_ME_PATH = "/api/auth/me/"
SESSION_LIST_PATH = "/api/listening/desktop/sessions/"

#: Bounds every request so a stalled backend cannot hang the local UI. Explicit
#: because httpx's default applies one value to connect, read and write alike,
#: and reading a hundred-segment page deserves more room than a TCP handshake.
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

#: Matches the backend's DESKTOP_SEGMENT_PAGE_SIZE (views.py), so the jump
#: computed by last_page_cursor lands on a cursor the backend accepts.
SEGMENT_PAGE_SIZE = 100


def last_page_cursor(segment_count: int, page_size: int) -> int:
    """The offset of the final page, on a page boundary.

    The backend validates cursors with ``offset % page_size == 0``, so the
    obvious ``segment_count - page_size`` is rejected as an invalid cursor for
    any count that is not itself a multiple of the page size.
    """
    if segment_count <= 0:
        return 0
    return ((segment_count - 1) // page_size) * page_size


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


class RemoteNotFoundError(RemoteRequestError):
    """The requested remote session is not visible to this desktop user."""


class RemoteConflictError(RemoteRequestError):
    """The requested remote state transition is currently unsafe."""


class RemoteValidationError(RemoteRequestError):
    """The remote rejected a session metadata update."""


class RemoteProtocolError(RemoteError):
    """The remote contract returned an unsupported or invalid event."""


@dataclass(frozen=True)
class SessionStarted:
    uuid_code: str
    next_sequence_by_channel: Mapping[str, int]
    max_duration_s: int
    #: Server-stamped, so the history entry this event produces sorts and
    #: renders like the ones the list endpoint returns. Optional because an
    #: older platform does not send them; the client then falls back to what
    #: it knew before, which is how it behaved all along.
    started_at: str | None = None
    last_activity_at: str | None = None


@dataclass(frozen=True)
class TranscriptDeltaEvent:
    """Legacy fake event retained while the local UI transition is completed."""

    channel: str
    utterance_id: str
    text: str
    started_offset_ms: int


@dataclass(frozen=True)
class TranscriptDiscardEvent:
    channel: str
    utterance_id: str
    reason: str


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
    | TranscriptDiscardEvent
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

    async def list_sessions(self, cursor: str | None, query: str) -> SessionPage: ...

    async def get_session(self, uuid_code: str) -> SessionSummary: ...

    async def list_segments(self, uuid_code: str, cursor: str | None) -> SegmentPage: ...

    async def last_segment_offset_ms(self, uuid_code: str, segment_count: int) -> int: ...

    async def update_session(
        self, uuid_code: str, *, title: str | None = None, is_pinned: bool | None = None
    ) -> SessionSummary: ...

    async def delete_session(self, uuid_code: str) -> None: ...

    async def connect_stream(
        self,
        *,
        resume_code: str | None,
        device_label: str,
        language_mic: str,
        language_system: str,
        title: str | None = None,
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
        proxy: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._websocket_path = websocket_path
        self._socket_factory = socket_factory or websockets.connect
        self._proxy = proxy
        self._client: httpx.AsyncClient | None = None

    @property
    def client_proxy(self) -> str | None:
        """The proxy URL this credential's requests are routed through, if any."""
        return self._proxy

    def _websocket_proxy(self) -> str | Literal[True]:
        """Resolve what ``websockets.connect`` should be told about proxies.

        ``websockets.connect`` signs its argument ``proxy: str | Literal[True] |
        None = True``, and ``None`` there means *disable proxy support*, not
        "no proxy configured". Passing ``self._proxy`` straight through would
        therefore switch off ``HTTPS_PROXY``/``WSS_PROXY`` for every user who
        never opened the settings panel -- a regression against the code that
        simply omitted the argument -- and would leave the audio transport
        disagreeing with the HTTP client, which still honours the environment
        through httpx's own ``trust_env``. So an unconfigured proxy restores
        the library default instead of suppressing it.
        """
        return self._proxy if self._proxy is not None else True

    async def aclose(self) -> None:
        """Release the pooled connections once this credential is done with."""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def verify_token(self) -> None:
        await self._request_json("GET", AUTH_ME_PATH)

    async def list_sessions(self, cursor: str | None, query: str) -> SessionPage:
        params = {key: value for key, value in {"cursor": cursor, "q": query}.items() if value}
        return _parse_session_page(
            await self._request_json("GET", _with_query(SESSION_LIST_PATH, params))
        )

    async def get_session(self, uuid_code: str) -> SessionSummary:
        payload = await self._request_json(
            "GET", f"{SESSION_LIST_PATH}{quote(uuid_code, safe='')}/"
        )
        return _parse_session(payload)

    async def list_segments(self, uuid_code: str, cursor: str | None) -> SegmentPage:
        path = f"{SESSION_LIST_PATH}{quote(uuid_code, safe='')}/segments/"
        return _parse_segment_page(
            await self._request_json("GET", _with_query(path, {"cursor": cursor}))
        )

    async def last_segment_offset_ms(self, uuid_code: str, segment_count: int) -> int:
        """Where a resumed session's new audio must start.

        Reads only the final page rather than walking the whole transcript --
        the offset comes from where the last stored segment ended, since that
        is the point in the meeting nothing has been captured past yet.
        """
        if segment_count <= 0:
            return 0
        cursor = last_page_cursor(segment_count, SEGMENT_PAGE_SIZE)
        page = await self.list_segments(uuid_code, cursor=str(cursor))
        return max((segment.ended_offset_ms for segment in page.segments), default=0)

    async def update_session(
        self, uuid_code: str, *, title: str | None = None, is_pinned: bool | None = None
    ) -> SessionSummary:
        payload: dict[str, str | bool] = {}
        if title is not None:
            payload["title"] = title
        if is_pinned is not None:
            payload["is_pinned"] = is_pinned
        if not payload:
            raise ValueError("A session update requires metadata.")
        return _parse_session(
            await self._request_json(
                "PATCH",
                f"{SESSION_LIST_PATH}{quote(uuid_code, safe='')}/",
                json_body=payload,
            )
        )

    async def delete_session(self, uuid_code: str) -> None:
        await self._request("DELETE", f"{SESSION_LIST_PATH}{quote(uuid_code, safe='')}/")

    async def connect_stream(
        self,
        *,
        resume_code: str | None,
        device_label: str,
        language_mic: str,
        language_system: str,
        title: str | None = None,
    ) -> RemoteStream:
        try:
            socket = await self._socket_factory(
                _stream_url(
                    self._base_url,
                    self._websocket_path,
                    resume_code=resume_code,
                    device_label=device_label,
                    language_mic=language_mic,
                    language_system=language_system,
                    title=title,
                ),
                additional_headers={"Authorization": _authorization_header(self._token)},
                proxy=self._websocket_proxy(),
            )
        except Exception as error:
            if _handshake_status_code(error) in {401, 403}:
                raise RemoteUnauthorizedError from None
            # A refusal that arrives as a close code rather than an HTTP status
            # carries the actual reason -- no credit, over the duration limit --
            # and collapsing it into RemoteRequestError threw that away.
            if _close_code(error) is not None:
                _raise_safe_remote_error(error)
            raise RemoteRequestError from None
        return _WebSocketRemoteStream(socket)

    def _http_client(self) -> httpx.AsyncClient:
        """Return this credential's client, keeping its connection pool alive.

        One client per request meant a fresh TLS handshake for every call --
        paid once per page while walking a long meeting's segments.

        ``transport`` and ``proxy`` are mutually exclusive in practice: httpx
        mounts a proxy transport that takes precedence over the ``transport``
        injected here, so a caller that sets both gets the proxy and silently
        loses the stub -- a test wired that way would reach the real network
        instead of its fake. Production sets only ``proxy`` and tests set only
        ``transport``; nothing should set both. Leaving ``proxy=None`` here
        when unset is correct for httpx (unlike websockets, see
        ``_websocket_proxy``): ``trust_env`` still applies, so the environment
        proxy is honoured.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": _authorization_header(self._token)},
                transport=self._transport,
                timeout=REQUEST_TIMEOUT,
                proxy=self._proxy,
            )
        return self._client

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            response = await self._http_client().request(method, path, json=json_body)
        except httpx.HTTPError:
            raise RemoteRequestError from None
        if response.status_code in {401, 403}:
            raise RemoteUnauthorizedError
        if response.status_code == 404:
            raise RemoteNotFoundError
        if response.status_code == 409:
            raise RemoteConflictError
        if 400 <= response.status_code < 500:
            raise RemoteValidationError
        if response.is_error:
            raise RemoteRequestError
        return response

    async def _request_json(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> object:
        response = await self._request(method, path, json_body=json_body)
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
                started_at=_optional_string(values, "started_at"),
                last_activity_at=_optional_string(values, "last_activity_at"),
            )
        if event_type == "transcript.segment":
            channel = _channel(values)
            sequence = self._sequence_for(channel)
            return TranscriptSegmentEvent(
                channel=channel,
                utterance_id=_optional_string(values, "utterance_id") or f"{channel}:{sequence}",
                text=_required_string(values, "text"),
                started_offset_ms=_required_integer(values, "started_offset_ms"),
                ended_offset_ms=_required_integer(values, "ended_offset_ms"),
            )
        if event_type == "transcript.delta":
            channel = _channel(values)
            sequence = self._current_sequence_for(channel)
            return TranscriptDeltaEvent(
                channel=channel,
                utterance_id=_optional_string(values, "utterance_id") or f"{channel}:{sequence}",
                text=_required_string(values, "text"),
                started_offset_ms=_required_integer(values, "started_offset_ms"),
            )
        if event_type == "transcript.discard":
            return TranscriptDiscardEvent(
                channel=_channel(values),
                utterance_id=_required_string(values, "utterance_id"),
                reason=_required_string(values, "reason"),
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


def _with_query(path: str, params: Mapping[str, str | None]) -> str:
    query = urlencode({key: value for key, value in params.items() if value})
    return f"{path}?{query}" if query else path


def _parse_session_page(payload: object) -> SessionPage:
    values = _object(payload)
    raw_sessions = values.get("sessions")
    if not isinstance(raw_sessions, list):
        raise RemoteProtocolError("Remote session list was invalid.")
    next_cursor = _optional_string(values, "next_cursor")
    return SessionPage(tuple(_parse_session(item) for item in raw_sessions), next_cursor)


def _parse_session(payload: object) -> SessionSummary:
    values = _object(payload)
    status = _required_string(values, "status")
    is_live = values.get("is_live")
    segment_count = values.get("segment_count")
    if (
        not isinstance(is_live, bool)
        or isinstance(segment_count, bool)
        or not isinstance(segment_count, int)
    ):
        raise RemoteProtocolError("Remote session payload was invalid.")
    title = values.get("title", "")
    device_label = values.get("device_label", "")
    is_pinned = values.get("is_pinned", False)
    pinned_at = _optional_string(values, "pinned_at")
    if (
        not isinstance(title, str)
        or not isinstance(device_label, str)
        or not isinstance(is_pinned, bool)
    ):
        raise RemoteProtocolError("Remote session payload was invalid.")
    return SessionSummary(
        uuid_code=_required_string(values, "uuid_code"),
        title=title,
        status=status,
        started_at=_optional_string(values, "started_at"),
        ended_at=_optional_string(values, "ended_at"),
        device_label=device_label,
        segment_count=segment_count,
        is_live=is_live,
        is_pinned=is_pinned,
        pinned_at=pinned_at,
        last_activity_at=_optional_string(values, "last_activity_at"),
    )


def _parse_segment_page(payload: object) -> SegmentPage:
    values = _object(payload)
    raw_segments = values.get("segments")
    if not isinstance(raw_segments, list):
        raise RemoteProtocolError("Remote segment list was invalid.")
    next_cursor = _optional_string(values, "next_cursor")
    return SegmentPage(tuple(_parse_segment(item) for item in raw_segments), next_cursor)


def _parse_segment(payload: object) -> TranscriptSegment:
    values = _object(payload)
    started_offset_ms = _required_integer(values, "started_offset_ms")
    ended_offset_ms = _required_integer(values, "ended_offset_ms")
    return TranscriptSegment(
        utterance_id=_required_string(values, "utterance_id"),
        channel=_channel(values),
        text=_required_string(values, "text"),
        started_offset_ms=started_offset_ms,
        ended_offset_ms=ended_offset_ms,
    )


def _stream_url(
    base_url: str,
    websocket_path: str,
    *,
    resume_code: str | None,
    device_label: str,
    language_mic: str,
    language_system: str,
    title: str | None,
) -> str:
    """Build the Listening handshake URL, dropping every value left empty.

    An omitted ``language_mic``/``language_system`` is what asks the service to
    detect that channel's language, so the empty string must not be sent as a
    forced language of its own.
    """
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
                "language_mic": language_mic,
                "language_system": language_system,
                "title": title,
                "features": "transcript_discard",
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


def _object(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, dict):
        raise RemoteProtocolError("Remote payload was invalid.")
    return payload


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise RemoteProtocolError("Remote payload was invalid.")
    return value


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
