from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from broccoli_desktop.remote import HttpListeningRemote, SessionStarted, TranscriptSegmentEvent


@dataclass
class FakeTransport(httpx.AsyncBaseTransport):
    responses: list[object] = field(default_factory=list)
    requests: list[tuple[str, str, str]] = field(default_factory=list)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path, request.headers["Authorization"]))
        return httpx.Response(200, json=self.responses.pop(0), request=request)


@dataclass
class FakeSocket:
    received: list[str] = field(default_factory=list)
    sent: list[str | bytes] = field(default_factory=list)
    closed: bool = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self.received:
            raise StopAsyncIteration
        return self.received.pop(0)

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeSocketFactory:
    socket: FakeSocket = field(default_factory=FakeSocket)
    url: str | None = None
    headers: dict[str, str] | None = None

    async def __call__(self, url: str, *, additional_headers: dict[str, str]) -> FakeSocket:
        self.url = url
        self.headers = additional_headers
        return self.socket


class ClosedSocketError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__("close detail that must not escape")
        self.code = code


@dataclass
class ClosingSocket(FakeSocket):
    close_code: int = 4401

    async def recv(self) -> str:
        raise ClosedSocketError(self.close_code)


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport(responses=[{"username": "desktop-user"}])


@pytest.fixture
def fake_socket_factory() -> FakeSocketFactory:
    return FakeSocketFactory()


@pytest.mark.asyncio
async def test_verify_token_uses_the_declared_token_endpoint(
    fake_transport: FakeTransport,
) -> None:
    remote = HttpListeningRemote(
        "http://127.0.0.1:8000",
        "secret",
        fake_transport,
        websocket_path="/ws/listening/",
    )

    await remote.verify_token()

    assert fake_transport.requests == [("GET", "/api/auth/me/", "Token secret")]


@pytest.mark.asyncio
async def test_stream_uses_handshake_query_and_derives_segment_id(
    fake_socket_factory: FakeSocketFactory,
) -> None:
    fake_socket_factory.socket.received = [
        json.dumps(
            {
                "type": "session.started",
                "uuid_code": "live-1",
                "resumed": False,
                "next_seq": {"mic": 4, "system": 2},
                "max_duration_s": 14_400,
            }
        ),
        json.dumps(
            {
                "type": "transcript.segment",
                "channel": "mic",
                "text": "hello",
                "started_offset_ms": 100,
                "ended_offset_ms": 900,
            }
        ),
    ]
    remote = HttpListeningRemote(
        "http://127.0.0.1:8000",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=fake_socket_factory,
    )

    stream = await remote.connect_stream(resume_code=None, device_label="Speakers", language="en")
    events = [event async for event in stream.events()]

    assert (
        fake_socket_factory.url == "ws://127.0.0.1:8000/ws/listening/?device=Speakers&language=en"
    )
    assert fake_socket_factory.socket.sent == []
    assert events == [
        SessionStarted("live-1", {"mic": 4, "system": 2}, 14_400),
        TranscriptSegmentEvent("mic", "mic:4", "hello", 100, 900),
    ]


@pytest.mark.asyncio
async def test_stream_includes_nonempty_resume_in_the_handshake_query(
    fake_socket_factory: FakeSocketFactory,
) -> None:
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=fake_socket_factory,
    )

    await remote.connect_stream(resume_code="live 1", device_label="Laptop speakers", language="en")

    assert (
        fake_socket_factory.url
        == "wss://broccoli.example/ws/listening/?resume=live+1&device=Laptop+speakers&language=en"
    )
    assert fake_socket_factory.headers == {"Authorization": "Token secret"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (4401, "RemoteUnauthorizedError"),
        (4402, "RemoteCreditError"),
        (4403, "RemoteDurationError"),
    ],
)
async def test_stream_exposes_listening_close_codes_as_safe_typed_errors(
    code: int, expected: str
) -> None:
    socket_factory = FakeSocketFactory(ClosingSocket(close_code=code))
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=socket_factory,
    )
    stream = await remote.connect_stream(resume_code=None, device_label="Laptop", language="en")

    with pytest.raises(Exception) as raised:
        _ = [event async for event in stream.events()]

    assert type(raised.value).__name__ == expected
    assert "close detail" not in str(raised.value)
