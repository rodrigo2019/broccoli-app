from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from broccoli_desktop.models import SegmentPage, SessionPage, SessionSummary, TranscriptSegment
from broccoli_desktop.remote import (
    SEGMENT_PAGE_SIZE,
    HttpListeningRemote,
    SessionStarted,
    TranscriptDeltaEvent,
    TranscriptSegmentEvent,
    last_page_cursor,
)


@dataclass
class FakeTransport(httpx.AsyncBaseTransport):
    responses: list[object] = field(default_factory=list)
    requests: list[tuple[str, str, str]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    json_bodies: list[object | None] = field(default_factory=list)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path, request.headers["Authorization"]))
        self.urls.append(str(request.url))
        self.json_bodies.append(json.loads(request.content) if request.content else None)
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
    proxy: str | None = None

    async def __call__(
        self, url: str, *, additional_headers: dict[str, str], proxy: str | None = None
    ) -> FakeSocket:
        self.url = url
        self.headers = additional_headers
        self.proxy = proxy
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
async def test_history_requests_parse_pages_and_keep_query_parameters() -> None:
    transport = FakeTransport(
        responses=[
            {
                "sessions": [
                    {
                        "uuid_code": "session-2",
                        "title": "",
                        "status": "ended",
                        "started_at": "2026-08-19T11:00:00Z",
                        "ended_at": "2026-08-19T12:00:00Z",
                        "device_label": "Speakers",
                        "segment_count": 1,
                        "is_live": False,
                    }
                ],
                "next_cursor": "20",
            },
            {
                "uuid_code": "session-2",
                "title": "",
                "status": "ended",
                "started_at": "2026-08-19T11:00:00Z",
                "ended_at": "2026-08-19T12:00:00Z",
                "device_label": "Speakers",
                "segment_count": 1,
                "is_live": False,
            },
            {
                "segments": [
                    {
                        "utterance_id": "system:1",
                        "channel": "system",
                        "text": "Hello",
                        "started_offset_ms": 100,
                        "ended_offset_ms": 900,
                    }
                ],
                "next_cursor": None,
            },
        ]
    )
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        transport,
        websocket_path="/ws/listening/",
    )

    page = await remote.list_sessions("20", "meeting")
    detail = await remote.get_session("session-2")
    segments = await remote.list_segments("session-2", None)

    assert page == SessionPage(
        (
            SessionSummary(
                uuid_code="session-2",
                title="",
                status="ended",
                started_at="2026-08-19T11:00:00Z",
                ended_at="2026-08-19T12:00:00Z",
                device_label="Speakers",
                segment_count=1,
                is_live=False,
            ),
        ),
        "20",
    )
    assert detail.uuid_code == "session-2"
    assert segments == SegmentPage(
        (TranscriptSegment("system:1", "system", "Hello", 100, 900),), None
    )
    assert transport.urls == [
        "https://broccoli.example/api/listening/desktop/sessions/?cursor=20&q=meeting",
        "https://broccoli.example/api/listening/desktop/sessions/session-2/",
        "https://broccoli.example/api/listening/desktop/sessions/session-2/segments/",
    ]


def test_the_last_segment_page_lands_on_a_cursor_boundary() -> None:
    """The backend rejects any cursor that is not a multiple of the page size
    (views.py:61), so max(0, count - page_size) would be Invalid cursor for a
    250-segment session."""
    assert last_page_cursor(250, SEGMENT_PAGE_SIZE) == 200
    assert last_page_cursor(100, SEGMENT_PAGE_SIZE) == 0
    assert last_page_cursor(101, SEGMENT_PAGE_SIZE) == 100
    assert last_page_cursor(0, SEGMENT_PAGE_SIZE) == 0


@pytest.mark.asyncio
async def test_last_segment_offset_ms_reads_only_the_final_page() -> None:
    """A 250-segment session must jump straight to cursor 200, not walk every
    page from the start -- and the offset comes from where the last segment
    ended, not where it started."""
    transport = FakeTransport(
        responses=[
            {
                "segments": [
                    {
                        "utterance_id": "system:200",
                        "channel": "system",
                        "text": "...",
                        "started_offset_ms": 1_200_000,
                        "ended_offset_ms": 1_235_000,
                    },
                    {
                        "utterance_id": "system:201",
                        "channel": "system",
                        "text": "...",
                        "started_offset_ms": 1_236_000,
                        "ended_offset_ms": 1_240_000,
                    },
                ],
                "next_cursor": None,
            }
        ]
    )
    remote = HttpListeningRemote(
        "https://broccoli.example", "secret", transport, websocket_path="/ws/listening/"
    )

    offset_ms = await remote.last_segment_offset_ms("history-1", 250)

    assert offset_ms == 1_240_000
    assert transport.urls == [
        "https://broccoli.example/api/listening/desktop/sessions/history-1/segments/?cursor=200"
    ]


@pytest.mark.asyncio
async def test_last_segment_offset_ms_makes_no_request_for_an_empty_session() -> None:
    transport = FakeTransport(responses=[])
    remote = HttpListeningRemote(
        "https://broccoli.example", "secret", transport, websocket_path="/ws/listening/"
    )

    offset_ms = await remote.last_segment_offset_ms("history-1", 0)

    assert offset_ms == 0
    assert transport.urls == []


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

    stream = await remote.connect_stream(
        resume_code=None, device_label="Speakers", language="en", title="Daily review"
    )
    events = [event async for event in stream.events()]

    assert (
        fake_socket_factory.url
        == "ws://127.0.0.1:8000/ws/listening/?device=Speakers&language=en&title=Daily+review"
    )
    assert fake_socket_factory.socket.sent == []
    assert events == [
        SessionStarted("live-1", {"mic": 4, "system": 2}, 14_400),
        TranscriptSegmentEvent("mic", "mic:4", "hello", 100, 900),
    ]


@pytest.mark.asyncio
async def test_stream_uses_the_pending_segment_id_for_transcript_deltas(
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
                "type": "transcript.delta",
                "channel": "mic",
                "text": "hello ",
                "started_offset_ms": 100,
            }
        ),
        json.dumps(
            {
                "type": "transcript.delta",
                "channel": "mic",
                "text": "hello world",
                "started_offset_ms": 100,
            }
        ),
        json.dumps(
            {
                "type": "transcript.segment",
                "channel": "mic",
                "text": "hello world",
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

    assert events == [
        SessionStarted("live-1", {"mic": 4, "system": 2}, 14_400),
        TranscriptDeltaEvent("mic", "mic:4", "hello ", 100),
        TranscriptDeltaEvent("mic", "mic:4", "hello world", 100),
        TranscriptSegmentEvent("mic", "mic:4", "hello world", 100, 900),
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


def test_the_remote_uses_the_configured_proxy() -> None:
    """Injection is the point of the whole feature -- a remote built with a
    proxy has to expose it, or nothing downstream can ever route through it.

    websocket_path is passed explicitly (unlike the brief's literal snippet):
    this repo deliberately keeps it a required keyword so a caller can never
    silently connect to the server root -- _configured_websocket_path raises
    rather than defaulting, and the constructor should not undo that.
    """
    remote = HttpListeningRemote(
        base_url="https://example.invalid",
        token="t",
        websocket_path="",
        proxy="http://user:pass@proxy.local:8080",
    )

    assert remote.client_proxy == "http://user:pass@proxy.local:8080"


def test_a_remote_built_without_a_proxy_has_none() -> None:
    remote = HttpListeningRemote(base_url="https://example.invalid", token="t", websocket_path="")

    assert remote.client_proxy is None


@pytest.mark.asyncio
async def test_the_websocket_stream_is_opened_through_the_configured_proxy(
    fake_socket_factory: FakeSocketFactory,
) -> None:
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        websocket_path="/ws/listening/",
        socket_factory=fake_socket_factory,
        proxy="http://user:pass@proxy.local:8080",
    )

    await remote.connect_stream(resume_code=None, device_label="Speakers", language="en")

    assert fake_socket_factory.proxy == "http://user:pass@proxy.local:8080"


@pytest.mark.asyncio
async def test_session_actions_send_persisted_metadata_and_delete_requests() -> None:
    summary = {
        "uuid_code": "session-2",
        "title": "Renamed",
        "status": "ended",
        "started_at": "2026-08-19T11:00:00Z",
        "ended_at": "2026-08-19T12:00:00Z",
        "device_label": "Speakers",
        "segment_count": 1,
        "is_live": False,
        "is_pinned": True,
        "pinned_at": "2026-08-20T10:00:00Z",
    }
    transport = FakeTransport(responses=[summary, {}])
    remote = HttpListeningRemote(
        "https://broccoli.example",
        "secret",
        transport,
        websocket_path="/ws/listening/",
    )

    updated = await remote.update_session("session-2", title="Renamed", is_pinned=True)
    await remote.delete_session("session-2")

    assert updated.title == "Renamed"
    assert updated.is_pinned is True
    assert transport.requests == [
        ("PATCH", "/api/listening/desktop/sessions/session-2/", "Token secret"),
        ("DELETE", "/api/listening/desktop/sessions/session-2/", "Token secret"),
    ]
    assert transport.json_bodies == [{"title": "Renamed", "is_pinned": True}, None]


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
