from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from broccoli_desktop.api import Services, create_app
from broccoli_desktop.credentials import CredentialStorageError
from broccoli_desktop.models import (
    ConnectionState,
    SessionPage,
    UiEvent,
)
from broccoli_desktop.remote import RemoteRequestError, RemoteUnauthorizedError
from tests.fakes import (
    VISUAL_TEST_TOKEN,
    FakeCaptureBackend,
    FakeSessionRemote,
    visual_test_remote_factory,
)


@dataclass
class FakeCredentials:
    token: str | None = None
    saved_tokens: list[str] = field(default_factory=list)
    deleted_count: int = 0
    unavailable: bool = False

    def load_token(self) -> str | None:
        if self.unavailable:
            raise CredentialStorageError("Credential storage is unavailable.")
        return self.token

    def save_token(self, token: str) -> None:
        if self.unavailable:
            raise CredentialStorageError("Credential storage is unavailable.")
        self.token = token
        self.saved_tokens.append(token)

    def delete_token(self) -> None:
        if self.unavailable:
            raise CredentialStorageError("Credential storage is unavailable.")
        self.token = None
        self.deleted_count += 1


@dataclass
class FakeRemoteFactory:
    remote: FakeSessionRemote = field(default_factory=FakeSessionRemote)
    created_tokens: list[str] = field(default_factory=list)

    def __call__(self, token: str) -> FakeSessionRemote:
        self.created_tokens.append(token)
        return self.remote


@dataclass
class RejectingRemoteFactory(FakeRemoteFactory):
    def __post_init__(self) -> None:
        self.remote.unauthorized = True


@pytest.fixture
def fake_credentials() -> FakeCredentials:
    return FakeCredentials()


@pytest.fixture
def fake_remote_factory() -> FakeRemoteFactory:
    return FakeRemoteFactory()


@pytest.fixture
def rejecting_remote_factory() -> RejectingRemoteFactory:
    return RejectingRemoteFactory()


@pytest.fixture
def fake_capture() -> FakeCaptureBackend:
    return FakeCaptureBackend()


@pytest.fixture
def services(
    fake_credentials: FakeCredentials,
    fake_remote_factory: FakeRemoteFactory,
    fake_capture: FakeCaptureBackend,
) -> Services:
    return Services(
        credentials=fake_credentials,
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
        loopback_port=8765,
    )


@pytest.fixture
def client(services: Services) -> TestClient:
    return TestClient(create_app(services), headers={"host": "127.0.0.1:8765"})


def login(client: TestClient, token: str = "candidate") -> None:
    response = client.post("/api/login", json={"token": token})

    assert response.status_code == 204


def test_root_serves_the_desktop_shell(client: TestClient) -> None:
    """The loopback root exposes the controls required by the desktop UI."""
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="loginView"' in response.text
    assert 'id="sessionLibrary"' in response.text
    assert 'id="transcriptTimeline"' in response.text
    assert "output.css" in response.text
    assert 'data-testid="token-input"' in response.text
    assert 'data-testid="login-submit"' in response.text
    assert 'data-testid="login-error"' in response.text
    assert 'data-testid="session-search"' in response.text
    assert 'data-testid="new-session"' in response.text
    assert 'data-testid="load-more"' in response.text
    assert 'data-testid="session-library"' in response.text
    assert 'data-testid="resume-session"' in response.text
    assert 'data-testid="stop-session"' in response.text
    assert 'data-testid="copy-session-code"' in response.text
    assert 'data-testid="open-broccoli"' in response.text
    assert 'data-testid="status-banner"' in response.text
    assert 'data-testid="transcript-timeline"' in response.text
    assert 'aria-label="Broccoli access token"' in response.text
    assert 'aria-label="Search meeting sessions"' in response.text
    assert 'aria-label="Microphone"' in response.text
    assert 'aria-label="System audio"' in response.text


@pytest.mark.asyncio
async def test_visual_ui_fakes_provide_only_deterministic_local_data() -> None:
    """Task 9 can drive the notebook without a remote service or audio hardware."""
    factory = visual_test_remote_factory()
    remote = factory(VISUAL_TEST_TOKEN)

    first_page = await remote.list_sessions(cursor=None, query="")
    second_page = await remote.list_sessions(cursor="history-2", query="")
    search_page = await remote.list_sessions(cursor=None, query="Daily")

    assert [session.title for session in first_page.sessions] == ["Daily"]
    assert first_page.next_cursor == "history-2"
    assert [session.title for session in second_page.sessions] == ["Planning"]
    assert [session.title for session in search_page.sessions] == ["Daily"]
    with pytest.raises(RemoteUnauthorizedError):
        await factory("bad-token").verify_token()


def test_login_verifies_before_storing_the_token(
    client: TestClient,
    fake_remote_factory: FakeRemoteFactory,
    fake_credentials: FakeCredentials,
) -> None:
    response = client.post("/api/login", json={"token": "candidate"})

    assert response.status_code == 204
    assert fake_remote_factory.created_tokens == ["candidate"]
    assert fake_credentials.saved_tokens == ["candidate"]


def test_login_does_not_store_a_rejected_token(
    fake_capture: FakeCaptureBackend,
    fake_credentials: FakeCredentials,
    rejecting_remote_factory: RejectingRemoteFactory,
) -> None:
    services = Services(
        credentials=fake_credentials,
        remote_factory=rejecting_remote_factory,
        capture_backend=fake_capture,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1"})

    response = client.post("/api/login", json={"token": "bad"})

    assert response.status_code == 401
    assert fake_credentials.saved_tokens == []
    assert fake_credentials.deleted_count == 1
    assert "bad" not in response.text


def test_bootstrap_exposes_only_local_state_and_the_first_session_page(
    client: TestClient,
    fake_credentials: FakeCredentials,
) -> None:
    login(client)

    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    assert response.json() == {
        "authenticated": True,
        "official_broccoli_url": "https://broccoli.bosch-digital-factory.com",
        "selected_devices": None,
        "state": "idle",
        "session": None,
        "sessions": {
            "sessions": [
                {
                    "uuid_code": "session-1",
                    "title": "Existing session",
                    "status": "live",
                    "started_at": "2026-08-19T10:00:00Z",
                    "ended_at": None,
                    "device_label": "Speakers",
                    "segment_count": 0,
                    "is_live": True,
                }
            ],
            "next_cursor": None,
        },
    }
    assert "candidate" not in response.text
    assert fake_credentials.token == "candidate"


def test_bootstrap_is_unauthenticated_without_a_stored_credential(client: TestClient) -> None:
    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    assert response.json() == {
        "authenticated": False,
        "official_broccoli_url": "https://broccoli.bosch-digital-factory.com",
        "selected_devices": None,
        "state": "idle",
        "session": None,
        "sessions": {"sessions": [], "next_cursor": None},
    }


def test_authenticated_routes_use_the_injected_remote_and_map_resources(
    client: TestClient,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    login(client)

    sessions = client.get("/api/sessions", params={"cursor": "after", "q": "existing"})
    session = client.get("/api/sessions/session-1")
    segments = client.get("/api/sessions/session-1/segments", params={"cursor": "later"})
    renamed = client.patch("/api/sessions/session-1", json={"title": " Renamed "})

    assert sessions.status_code == 200
    assert session.status_code == 200
    assert segments.status_code == 200
    assert renamed.status_code == 200
    assert sessions.json()["sessions"][0]["uuid_code"] == "session-1"
    assert session.json()["title"] == "Existing session"
    assert segments.json() == {"segments": [], "next_cursor": None}
    assert renamed.json()["title"] == "Renamed"
    assert fake_remote_factory.remote.sessions["session-1"].title == "Renamed"


def test_title_updates_are_normalized_before_reaching_the_remote(
    client: TestClient,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    login(client)
    received_titles: list[str] = []
    original_update_title = fake_remote_factory.remote.update_title

    async def record_title(uuid_code: str, title: str):
        received_titles.append(title)
        return await original_update_title(uuid_code, title)

    fake_remote_factory.remote.update_title = record_title  # type: ignore[method-assign]
    response = client.patch("/api/sessions/session-1", json={"title": " Renamed "})

    assert response.status_code == 200
    assert received_titles == ["Renamed"]


def test_session_actions_validate_devices_titles_and_local_state(
    client: TestClient,
    services: Services,
) -> None:
    login(client)

    unknown_device = client.post(
        "/api/sessions",
        json={"title": "Daily", "microphone_id": "missing", "system_device_id": "system-1"},
    )
    wrong_kind = client.post(
        "/api/sessions",
        json={"title": "Daily", "microphone_id": "system-1", "system_device_id": "mic-1"},
    )
    bad_title = client.patch("/api/sessions/session-1", json={"title": "x" * 121})
    started = client.post(
        "/api/sessions",
        json={"title": " Daily ", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    duplicate = client.post(
        "/api/sessions/session-1/resume",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    stopped = client.post("/api/sessions/stop")

    assert unknown_device.status_code == 422
    assert wrong_kind.status_code == 422
    assert bad_title.status_code == 422
    assert started.status_code == 201
    assert started.json()["title"] == "Daily"
    assert duplicate.status_code == 409
    assert stopped.status_code == 204
    assert services.controller is not None
    assert services.controller.state is ConnectionState.STOPPED


def test_devices_and_logout_use_local_dependencies_only(
    client: TestClient,
    fake_credentials: FakeCredentials,
) -> None:
    login(client)

    devices = client.get("/api/devices")
    logout = client.delete("/api/login")

    assert devices.status_code == 200
    assert devices.json() == {
        "devices": [
            {"device_id": "mic-1", "label": "Microphone One", "kind": "mic"},
            {"device_id": "system-1", "label": "Speakers", "kind": "system"},
        ]
    }
    assert logout.status_code == 204
    assert fake_credentials.token is None


def test_remote_auth_failure_deletes_the_credential_and_returns_401(
    client: TestClient,
    fake_credentials: FakeCredentials,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    login(client)
    fake_remote_factory.remote.revoke_token()

    response = client.get("/api/sessions")

    assert response.status_code == 401
    assert fake_credentials.token is None
    assert response.json() == {"detail": "Authentication is required."}


def test_remote_outage_is_a_recoverable_503(
    client: TestClient,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    login(client)

    async def unavailable(*, cursor: str | None, query: str) -> SessionPage:
        assert cursor is None
        assert query == ""
        raise RemoteRequestError()

    fake_remote_factory.remote.list_sessions = unavailable  # type: ignore[method-assign]
    response = client.get("/api/sessions")

    assert response.status_code == 503
    assert response.json() == {"detail": "The remote service is unavailable."}


def test_keyring_outage_is_reported_without_exposing_credential_details(
    fake_capture: FakeCaptureBackend,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    services = Services(
        credentials=FakeCredentials(unavailable=True),
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1"})

    response = client.get("/api/bootstrap")

    assert response.status_code == 503
    assert response.json() == {"detail": "Credential storage is unavailable."}


def test_host_header_must_name_the_local_loopback_service(client: TestClient) -> None:
    allowed = client.get("/api/bootstrap", headers={"host": "LOCALHOST:8765"})
    response = client.get("/api/bootstrap", headers={"host": "example.invalid"})

    assert allowed.status_code == 200
    assert response.status_code == 400
    assert response.json() == {"detail": "Local host required."}


def test_event_socket_sends_a_safe_bootstrap_then_one_way_ui_events(
    client: TestClient,
    services: Services,
) -> None:
    login(client)

    with client.websocket_connect("/api/events") as websocket:
        bootstrap = websocket.receive_json()
        websocket.send_text('{"type":"remote.proxy","token":"candidate"}')
        assert services.controller is not None
        services.controller.events.publish(
            UiEvent(type="warning", message="Session credits are running low.")
        )
        event = websocket.receive_json()

    assert bootstrap["type"] == "bootstrap"
    assert bootstrap["bootstrap"]["authenticated"] is True
    assert bootstrap["bootstrap"]["sessions"]["sessions"] == [
        {
            "uuid_code": "session-1",
            "title": "Existing session",
            "status": "live",
            "started_at": "2026-08-19T10:00:00Z",
            "ended_at": None,
            "device_label": "Speakers",
            "segment_count": 0,
            "is_live": True,
        }
    ]
    assert event == {"type": "warning", "message": "Session credits are running low."}
    assert "candidate" not in str([bootstrap, event])


def test_invalid_request_errors_do_not_echo_submitted_tokens(client: TestClient) -> None:
    response = client.post("/api/login", json={"token": {"secret": "candidate"}})

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request."}
    assert "candidate" not in response.text
