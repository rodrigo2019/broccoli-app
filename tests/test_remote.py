from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from broccoli_desktop.remote import (
    HttpListeningRemote,
    RemoteProtocolError,
    RemoteRequestError,
    RemoteUnauthorizedError,
    SessionStarted,
    TranscriptDeltaEvent,
    TranscriptSegmentEvent,
)
from tests.fakes import (
    delta_final_pair,
    remote_close,
    resumed_session_started,
    revoked_remote,
    unauthorized_remote,
)


@dataclass
class FakeTransport(httpx.AsyncBaseTransport):
    responses: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.responses.pop(0), request=request)


@dataclass
class FakeSocket:
    messages: list[str]
    sent_bytes: list[bytes] = field(default_factory=list)
    sent_text: list[str] = field(default_factory=list)
    closed: bool = False

    async def send(self, message: bytes | str) -> None:
        if isinstance(message, bytes):
            self.sent_bytes.append(message)
        else:
            self.sent_text.append(message)

    async def recv(self) -> str:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self) -> None:
        self.closed = True


@dataclass
class FailingSocket(FakeSocket):
    secret_text: str = "server payload that must not escape"
    fail_send: bool = False
    fail_close: bool = False

    async def send(self, message: bytes | str) -> None:
        if self.fail_send:
            raise RuntimeError(self.secret_text)
        await super().send(message)

    async def close(self) -> None:
        if self.fail_close:
            raise RuntimeError(self.secret_text)
        await super().close()


@dataclass
class FakeSocketFactory:
    socket: FakeSocket
    urls: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    async def __call__(self, url: str, *, additional_headers: dict[str, str]) -> FakeSocket:
        self.urls.append(url)
        self.headers.append(additional_headers)
        return self.socket


class HandshakeError(Exception):
    """Fake handshake failure whose payload-like message must not escape."""

    def __init__(self, *, status_code: int | None = None, response: object | None = None) -> None:
        super().__init__("remote handshake payload that must not escape")
        self.status_code = status_code
        self.response = response


@dataclass(frozen=True)
class HandshakeResponse:
    status_code: int


@dataclass
class FailingSocketFactory:
    error: Exception

    async def __call__(self, _url: str, *, additional_headers: dict[str, str]) -> FakeSocket:
        del additional_headers
        raise self.error


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport(
        responses=[
            {
                "sessions": [
                    {
                        "uuid_code": "session-1",
                        "title": "Daily",
                        "status": "ended",
                        "started_at": "2026-08-19T10:00:00Z",
                        "ended_at": "2026-08-19T10:30:00Z",
                        "device_label": "Laptop",
                        "segment_count": 1,
                        "is_live": False,
                    }
                ],
                "next_cursor": "cursor-2",
            }
        ]
    )


@pytest.fixture
def fake_socket_factory() -> FakeSocketFactory:
    return FakeSocketFactory(
        FakeSocket(
            messages=[
                json.dumps(
                    {
                        "type": "transcript.delta",
                        "channel": "mic",
                        "utterance_id": "utterance-1",
                        "text": "we should",
                        "started_offset_ms": 500,
                    }
                ),
                json.dumps(
                    {
                        "type": "transcript.segment",
                        "channel": "mic",
                        "utterance_id": "utterance-1",
                        "text": "we should ship",
                        "started_offset_ms": 500,
                        "ended_offset_ms": 1_000,
                    }
                ),
            ]
        )
    )


@pytest.mark.asyncio
async def test_list_sessions_sends_token_header_and_maps_cursor_page(
    fake_transport: FakeTransport,
) -> None:
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        fake_transport,
        websocket_path="/ws/listening/",
    )

    page = await remote.list_sessions(cursor="next", query="daily")

    assert fake_transport.requests[0].headers["Authorization"] == "Token secret"
    assert fake_transport.requests[0].url.path == "/api/listening/desktop/sessions/"
    assert str(fake_transport.requests[0].url.query, "ascii") == "cursor=next&q=daily"
    assert page.next_cursor == "cursor-2"
    assert page.sessions[0].uuid_code == "session-1"


@pytest.mark.asyncio
async def test_rest_resources_map_sessions_segments_and_title_without_network() -> None:
    transport = FakeTransport(
        responses=[
            {
                "sessions": [],
                "next_cursor": None,
            },
            {
                "uuid_code": "session-1",
                "title": "Daily",
                "status": "ended",
                "started_at": "2026-08-19T10:00:00Z",
                "ended_at": "2026-08-19T10:30:00Z",
                "device_label": "Laptop",
                "segment_count": 1,
                "is_live": False,
            },
            {
                "segments": [
                    {
                        "utterance_id": "utterance-1",
                        "channel": "system",
                        "text": "final words",
                        "started_offset_ms": 500,
                        "ended_offset_ms": 1_000,
                    }
                ],
                "next_cursor": "more",
            },
            {
                "uuid_code": "session-1",
                "title": "Renamed",
                "status": "ended",
                "started_at": "2026-08-19T10:00:00Z",
                "ended_at": "2026-08-19T10:30:00Z",
                "device_label": "Laptop",
                "segment_count": 1,
                "is_live": False,
            },
        ]
    )
    remote = HttpListeningRemote(
        "http://broccoli.example",
        "secret",
        transport,
        websocket_path="/ws/listening/",
    )

    verified = await remote.verify_token()
    session = await remote.get_session("session-1")
    segments = await remote.list_segments("session-1", cursor="after-1")
    renamed = await remote.update_title("session-1", "Renamed")

    assert verified.sessions == ()
    assert session.title == "Daily"
    assert segments.next_cursor == "more"
    assert segments.segments[0].utterance_id == "utterance-1"
    assert renamed.title == "Renamed"
    assert [(request.method, request.url.path) for request in transport.requests] == [
        ("GET", "/api/listening/desktop/sessions/"),
        ("GET", "/api/listening/desktop/sessions/session-1/"),
        ("GET", "/api/listening/desktop/sessions/session-1/segments/"),
        ("PATCH", "/api/listening/desktop/sessions/session-1/"),
    ]
    assert str(transport.requests[2].url.query, "ascii") == "cursor=after-1"
    assert json.loads(transport.requests[3].content) == {"title": "Renamed"}


@pytest.mark.asyncio
async def test_remote_delta_and_segment_share_the_utterance_identifier(
    fake_socket_factory: FakeSocketFactory,
) -> None:
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/backend-owner-confirmed/",
        socket_factory=fake_socket_factory,
    )

    stream = await remote.connect_stream(resume_code="session-1", device_label="Laptop")
    events = [event async for event in stream.events()]

    assert isinstance(events[0], TranscriptDeltaEvent)
    assert isinstance(events[1], TranscriptSegmentEvent)
    assert events[0].utterance_id == events[1].utterance_id
    assert fake_socket_factory.urls == ["wss://broccoli.example/backend-owner-confirmed/"]
    assert fake_socket_factory.headers == [{"Authorization": "Token secret"}]
    assert json.loads(fake_socket_factory.socket.sent_text[0]) == {
        "type": "session.start",
        "resume_code": "session-1",
        "device_label": "Laptop",
    }


@pytest.mark.asyncio
async def test_stream_parses_a_started_event_with_resume_offset() -> None:
    socket_factory = FakeSocketFactory(
        FakeSocket(
            messages=[
                json.dumps(
                    {
                        "type": "session.started",
                        "uuid_code": "session-1",
                        "next_seq": 12,
                        "next_offset_ms": 45_000,
                        "max_duration_s": 14_400,
                    }
                )
            ]
        )
    )
    remote = HttpListeningRemote(
        "http://broccoli.example",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=socket_factory,
    )

    stream = await remote.connect_stream(resume_code="session-1", device_label="Laptop")
    events = [event async for event in stream.events()]

    assert events == [
        SessionStarted(
            uuid_code="session-1",
            next_seq=12,
            next_offset_ms=45_000,
            max_duration_s=14_400,
        )
    ]
    assert socket_factory.urls == ["ws://broccoli.example/ws/listening/"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        HandshakeError(status_code=401),
        HandshakeError(response=HandshakeResponse(status_code=403)),
    ],
)
async def test_authenticated_websocket_handshake_failure_is_mapped_without_payload(
    error: HandshakeError,
) -> None:
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/backend-owner-confirmed/",
        socket_factory=FailingSocketFactory(error),
    )

    with pytest.raises(RemoteUnauthorizedError) as raised:
        await remote.connect_stream(resume_code=None, device_label="Laptop")

    assert "remote handshake payload" not in str(raised.value)


@pytest.mark.asyncio
async def test_non_authentication_websocket_handshake_failure_remains_a_request_error() -> None:
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/backend-owner-confirmed/",
        socket_factory=FailingSocketFactory(HandshakeError(status_code=502)),
    )

    with pytest.raises(RemoteRequestError) as raised:
        await remote.connect_stream(resume_code=None, device_label="Laptop")

    assert "remote handshake payload" not in str(raised.value)


@pytest.mark.asyncio
async def test_unknown_event_reports_only_its_type_not_remote_payload() -> None:
    secret_text = "remote text that must not escape"
    socket_factory = FakeSocketFactory(
        FakeSocket(messages=[json.dumps({"type": "unknown.event", "text": secret_text})])
    )
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=socket_factory,
    )
    stream = await remote.connect_stream(resume_code=None, device_label="Laptop")

    with pytest.raises(RemoteProtocolError) as error:
        _ = [event async for event in stream.events()]

    assert str(error.value) == "Unexpected remote event type: unknown.event"
    assert secret_text not in str(error.value)


@pytest.mark.asyncio
async def test_stream_sends_binary_frames_control_messages_and_closes(
    fake_socket_factory: FakeSocketFactory,
) -> None:
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=fake_socket_factory,
    )
    stream = await remote.connect_stream(resume_code=None, device_label="Laptop")

    await stream.send_bytes(b"frame")
    await stream.send_control({"type": "session.end"})
    await stream.close()

    assert fake_socket_factory.socket.sent_bytes == [b"frame"]
    assert json.loads(fake_socket_factory.socket.sent_text[-1]) == {"type": "session.end"}
    assert fake_socket_factory.socket.closed is True


@pytest.mark.asyncio
async def test_socket_send_and_close_failures_are_sanitized() -> None:
    secret_text = "server payload that must not escape"
    initial_factory = FakeSocketFactory(FailingSocket(messages=[], fail_send=True))
    initial_remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=initial_factory,
    )

    with pytest.raises(Exception) as initial_error:
        await initial_remote.connect_stream(resume_code=None, device_label="Laptop")

    assert str(initial_error.value) == "Remote request failed."
    assert secret_text not in str(initial_error.value)

    socket = FailingSocket(messages=[])
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=FakeSocketFactory(socket),
    )
    stream = await remote.connect_stream(resume_code=None, device_label="Laptop")
    socket.fail_send = True

    with pytest.raises(Exception) as send_error:
        await stream.send_bytes(b"frame")
    with pytest.raises(Exception) as control_error:
        await stream.send_control({"type": "session.end"})
    socket.fail_send = False
    socket.fail_close = True
    with pytest.raises(Exception) as close_error:
        await stream.close()

    for error in (send_error.value, control_error.value, close_error.value):
        assert str(error) == "Remote request failed."
        assert secret_text not in str(error)


@pytest.mark.asyncio
async def test_deterministic_remote_fakes_model_auth_resume_delta_and_close() -> None:
    with pytest.raises(Exception, match="authentication"):
        await unauthorized_remote().verify_token()
    with pytest.raises(Exception, match="authentication"):
        await revoked_remote().verify_token()

    started = resumed_session_started()
    delta, segment = delta_final_pair()
    stream = remote_close([started, delta, segment])
    events: list[object] = []
    with pytest.raises(Exception, match="closed"):
        async for event in stream.events():
            events.append(event)

    assert started.next_offset_ms > 0
    assert delta.utterance_id == segment.utterance_id
    assert events == [started, delta, segment]
