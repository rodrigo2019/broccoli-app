from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broccoli_desktop.api import Services, _audio_level_events, create_app
from broccoli_desktop.credentials import CredentialStorageError
from broccoli_desktop.models import (
    ConnectionState,
    SegmentPage,
    SessionPage,
    SessionSummary,
    TranscriptSegment,
    UiEvent,
)
from broccoli_desktop.session import CaptureChoices, DesktopSessionController
from tests.fakes import (
    VISUAL_TEST_TOKEN,
    FakeCaptureBackend,
    FakeClock,
    FakeSessionRemote,
    visual_test_remote,
)
from tests.visual_server import create_visual_app


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
class FakeDeviceSettings:
    """In-memory selection storage that can never write to a user profile."""

    selection: CaptureChoices | None = None
    saved: list[CaptureChoices] = field(default_factory=list)
    clear_count: int = 0

    def load(self) -> CaptureChoices | None:
        return self.selection

    def save(self, selection: CaptureChoices) -> None:
        self.selection = selection
        self.saved.append(selection)

    def clear(self) -> None:
        self.selection = None
        self.clear_count += 1


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


def login_with_visual_token(client: TestClient) -> None:
    """Authenticate an in-process visual server with its fixed fake-only credential."""
    response = client.post("/api/login", json={"token": VISUAL_TEST_TOKEN})

    assert response.status_code == 204


def test_fake_bootstrap_exposes_the_same_capability_shape() -> None:
    """The browser visual server must expose the same history contract."""
    client = TestClient(create_visual_app(port=8765), headers={"host": "127.0.0.1:8765"})
    login_with_visual_token(client)

    assert client.get("/api/bootstrap").json()["capabilities"] == {
        "history": True,
        "remote_title": True,
        "session_actions": True,
        "user_resume": True,
        "segment_history": True,
    }


def test_visual_fake_does_not_seed_removed_history_workflow() -> None:
    """The deterministic visual remote must start fresh rather than model browse/resume history."""
    remote = visual_test_remote()

    assert asyncio.run(remote.list_sessions(None, "")).sessions == ()


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
    assert 'data-testid="new-session"' in response.text
    assert 'data-testid="settings-button"' in response.text
    assert 'id="settingsView"' in response.text
    assert 'id="settingsHeaderTitle"' in response.text
    assert 'id="closeSettingsButton"' not in response.text
    assert "Mock local" not in response.text
    assert 'data-testid="proxy-toggle"' in response.text
    assert 'data-testid="test-audio"' in response.text
    assert 'data-testid="session-library"' in response.text
    assert 'data-testid="capture-toggle"' in response.text
    assert 'data-testid="copy-session-code"' in response.text
    assert 'data-testid="open-broccoli"' in response.text
    assert 'id="renameSessionModal"' in response.text
    assert 'id="deleteSessionModal"' in response.text
    assert 'data-testid="status-banner"' in response.text
    assert 'data-testid="transcript-timeline"' in response.text
    assert 'aria-label="Broccoli access token"' in response.text
    assert 'data-testid="session-search"' not in response.text
    assert 'id="settingsMicrophoneSelect"' in response.text
    assert 'id="settingsSystemDeviceSelect"' in response.text


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


def test_bootstrap_exposes_history_features(
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
        "sessions": {"sessions": [], "next_cursor": None},
        "capabilities": {
            "history": True,
            "remote_title": True,
            "session_actions": True,
            "user_resume": True,
            "segment_history": True,
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
        "capabilities": {
            "history": True,
            "remote_title": True,
            "session_actions": True,
            "user_resume": True,
            "segment_history": True,
        },
    }


def test_history_routes_proxy_session_and_segment_pages(
    client: TestClient,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    login(client)

    session = SessionSummary(
        uuid_code="session-2",
        title="Local title",
        status="ended",
        started_at="2026-08-19T11:00:00Z",
        ended_at="2026-08-19T12:00:00Z",
        device_label="Meeting speakers",
        segment_count=1,
        is_live=False,
    )
    segment = TranscriptSegment("system:1", "system", "A sentence", 100, 900)
    fake_remote_factory.remote.session_pages[(None, "meeting")] = SessionPage((session,), "20")
    fake_remote_factory.remote.sessions[session.uuid_code] = session
    fake_remote_factory.remote.segment_pages[(session.uuid_code, None)] = SegmentPage(
        (segment,), None
    )

    listed = client.get("/api/sessions?q=meeting")
    detail = client.get(f"/api/sessions/{session.uuid_code}")
    segments = client.get(f"/api/sessions/{session.uuid_code}/segments")

    assert listed.status_code == 200
    assert listed.json() == {
        "sessions": [
            {
                "uuid_code": "session-2",
                "title": "Local title",
                "status": "ended",
                "started_at": "2026-08-19T11:00:00Z",
                "ended_at": "2026-08-19T12:00:00Z",
                "device_label": "Meeting speakers",
                "segment_count": 1,
                "is_live": False,
                "is_pinned": False,
                "pinned_at": None,
            }
        ],
        "next_cursor": "20",
    }
    assert detail.status_code == 200
    assert detail.json()["uuid_code"] == session.uuid_code
    assert segments.status_code == 200
    assert segments.json() == {
        "segments": [
            {
                "utterance_id": "system:1",
                "channel": "system",
                "text": "A sentence",
                "started_offset_ms": 100,
                "ended_offset_ms": 900,
            }
        ],
        "next_cursor": None,
    }


def test_session_actions_proxy_metadata_updates_and_delete(
    client: TestClient, fake_remote_factory: FakeRemoteFactory
) -> None:
    login(client)

    renamed = client.patch("/api/sessions/session-1", json={"title": "Renamed"})
    pinned = client.patch("/api/sessions/session-1", json={"is_pinned": True})
    deleted = client.delete("/api/sessions/session-1")

    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed"
    assert pinned.status_code == 200
    assert pinned.json()["is_pinned"] is True
    assert pinned.json()["pinned_at"] is not None
    assert deleted.status_code == 204
    assert "session-1" not in fake_remote_factory.remote.sessions


def test_resume_route_reuses_the_current_session_after_a_client_stop(
    client: TestClient,
    services: Services,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    login(client)

    started = client.post(
        "/api/sessions",
        json={"title": "Daily", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    assert started.status_code == 201
    stopped = client.post("/api/sessions/stop")
    assert stopped.status_code == 204

    resumed = client.post(
        f"/api/sessions/{started.json()['uuid_code']}/resume",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )

    assert resumed.status_code == 201
    assert resumed.json()["uuid_code"] == started.json()["uuid_code"]
    assert resumed.json()["title"] == "Daily"
    assert fake_remote_factory.remote.stream_requests == [
        (None, "Speakers"),
        (started.json()["uuid_code"], "Speakers"),
    ]
    assert services.controller is not None
    assert services.controller.state is ConnectionState.STREAMING

    client.post("/api/sessions/stop")


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
    started = client.post(
        "/api/sessions",
        json={"title": " Daily ", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    stopped = client.post("/api/sessions/stop")

    assert unknown_device.status_code == 422
    assert wrong_kind.status_code == 422
    assert started.status_code == 201
    assert started.json()["title"] == "Daily"
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


def test_audio_level_routes_read_selected_devices_without_returning_pcm(
    client: TestClient,
    fake_capture: FakeCaptureBackend,
) -> None:
    unauthenticated = client.post(
        "/api/audio-levels",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    login(client)

    started = client.post(
        "/api/audio-levels",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    fake_capture.handles["mic-1"].emit(b"\xff\x7f")
    fake_capture.handles["system-1"].emit(b"\x00\x40")
    levels = client.get("/api/audio-levels")
    stopped = client.delete("/api/audio-levels")

    assert unauthenticated.status_code == 401
    assert started.status_code == 200
    assert started.json() == {
        "active": True,
        "microphone": {"level": 0.0, "peak": 0.0},
        "system": {"level": 0.0, "peak": 0.0},
    }
    assert levels.status_code == 200
    assert levels.json()["active"] is True
    assert levels.json()["microphone"]["level"] > 0.99
    assert 0.49 < levels.json()["system"]["peak"] < 0.51
    assert b"\xff\x7f" not in levels.content
    assert stopped.status_code == 204
    assert fake_capture.closed_sources == {"mic-1", "system-1"}


@pytest.mark.asyncio
async def test_audio_level_stream_sends_initial_and_live_snapshots(
    client: TestClient,
    services: Services,
    fake_capture: FakeCaptureBackend,
) -> None:
    assert client.get("/api/audio-levels/stream").status_code == 401
    login(client)
    services.start_audio_level_monitor(CaptureChoices("mic-1", "system-1"))

    events = _audio_level_events(services.audio_levels)
    initial = await anext(events)
    assert initial.startswith("data: {")
    assert '"active":true' in initial

    fake_capture.handles["mic-1"].emit(b"\xff\x7f")
    update = await asyncio.wait_for(anext(events), timeout=1)
    assert update.startswith("data: {")
    assert '"active":true' in update
    await events.aclose()
    services.stop_audio_level_monitor()


def test_audio_level_check_stops_before_a_capture_uses_the_devices(
    client: TestClient,
    fake_capture: FakeCaptureBackend,
) -> None:
    login(client)
    meter = client.post(
        "/api/audio-levels",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    capture = client.post(
        "/api/sessions",
        json={"title": "", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )

    assert meter.status_code == 200
    assert capture.status_code == 201
    assert fake_capture.closed_sources == {"mic-1", "system-1"}

    client.post("/api/sessions/stop")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/sessions", {"title": "", "microphone_id": "mic-1", "system_device_id": "system-1"}),
    ],
)
def test_stream_start_auth_failure_deletes_credentials_and_returns_401(
    client: TestClient,
    fake_credentials: FakeCredentials,
    fake_remote_factory: FakeRemoteFactory,
    path: str,
    body: dict[str, str],
) -> None:
    login(client)
    fake_remote_factory.remote.revoke_token()

    response = client.post(path, json=body)

    assert response.status_code == 401
    assert fake_credentials.token is None
    assert response.json() == {"detail": "Authentication is required."}


@pytest.mark.asyncio
async def test_background_recovery_auth_failure_clears_the_service_credential(
    fake_credentials: FakeCredentials,
    fake_remote_factory: FakeRemoteFactory,
    fake_capture: FakeCaptureBackend,
) -> None:
    services = Services(
        credentials=fake_credentials,
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
        controller_factory=lambda remote, capture: DesktopSessionController(
            remote, capture, clock=FakeClock()
        ),
    )
    fake_credentials.save_token("candidate")
    controller, _remote = services.authenticated()  # type: ignore[misc]
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")
    fake_remote_factory.remote.revoke_token()

    await fake_remote_factory.remote.emit_failure()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert fake_credentials.token is None
    assert fake_credentials.deleted_count == 1
    assert services.controller is None


def test_successful_capture_saves_opaque_device_choices_for_a_fresh_service(
    fake_credentials: FakeCredentials,
    fake_remote_factory: FakeRemoteFactory,
    fake_capture: FakeCaptureBackend,
) -> None:
    settings = FakeDeviceSettings()
    services = Services(
        credentials=fake_credentials,
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
        device_settings=settings,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1"})
    login(client)

    started = client.post(
        "/api/sessions",
        json={"title": "", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    stopped = client.post("/api/sessions/stop")
    fresh_services = Services(
        credentials=fake_credentials,
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
        device_settings=settings,
    )
    fresh_client = TestClient(create_app(fresh_services), headers={"host": "127.0.0.1"})
    bootstrap = fresh_client.get("/api/bootstrap")

    assert started.status_code == 201
    assert stopped.status_code == 204
    assert settings.saved == [CaptureChoices("mic-1", "system-1")]
    assert bootstrap.json()["selected_devices"] == {
        "microphone_id": "mic-1",
        "system_device_id": "system-1",
    }


def test_settings_can_save_and_clear_device_choices_before_a_capture_starts(
    fake_credentials: FakeCredentials,
    fake_remote_factory: FakeRemoteFactory,
    fake_capture: FakeCaptureBackend,
) -> None:
    settings = FakeDeviceSettings()
    services = Services(
        credentials=fake_credentials,
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
        device_settings=settings,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1"})
    login(client)

    saved = client.put(
        "/api/devices/selection",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    reopened_services = Services(
        credentials=fake_credentials,
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
        device_settings=settings,
    )
    reopened_client = TestClient(create_app(reopened_services), headers={"host": "127.0.0.1"})
    reopened = reopened_client.get("/api/bootstrap")
    cleared = client.delete("/api/devices/selection")

    assert saved.status_code == 204
    assert settings.saved == [CaptureChoices("mic-1", "system-1")]
    assert reopened.json()["selected_devices"] == {
        "microphone_id": "mic-1",
        "system_device_id": "system-1",
    }
    assert services.controller is not None
    assert services.controller.selected_devices is None
    assert cleared.status_code == 204
    assert settings.selection is None
    assert settings.clear_count == 1


def test_missing_persisted_device_selection_is_cleared_and_requires_replacement(
    fake_credentials: FakeCredentials,
    fake_remote_factory: FakeRemoteFactory,
    fake_capture: FakeCaptureBackend,
) -> None:
    settings = FakeDeviceSettings(selection=CaptureChoices("mic-1", "system-1"))
    fake_capture.devices = [
        device for device in fake_capture.devices if device.device_id != "system-1"
    ]
    services = Services(
        credentials=fake_credentials,
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
        device_settings=settings,
    )
    fake_credentials.save_token("candidate")
    client = TestClient(create_app(services), headers={"host": "127.0.0.1"})

    bootstrap = client.get("/api/bootstrap")
    start = client.post(
        "/api/sessions",
        json={"title": "", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )

    assert bootstrap.json()["selected_devices"] is None
    assert settings.selection is None
    assert settings.clear_count == 1
    assert start.status_code == 422


def test_static_client_renders_each_transcript_row_with_its_event_offset_timestamp() -> None:
    source = (Path(__file__).parents[1] / "broccoli_desktop" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert "function formatTranscriptTimestamp(offsetMs)" in source
    assert "timestamp.textContent = formatTranscriptTimestamp(entry.started_offset_ms);" in source
    assert "heading.append(speaker, timestamp);" in source
    assert "content.append(heading, text);" in source
    assert "row.append(avatar, content);" in source


def test_notebook_contains_history_loading_and_selection_workflow() -> None:
    source = Path("broccoli_desktop/static/app.js").read_text(encoding="utf-8")

    assert "async function loadSessions" in source
    assert "async function selectSession" in source
    assert "capabilities.segment_history" in source


def test_notebook_contains_session_action_menu_and_safe_live_delete_gate() -> None:
    source = Path("broccoli_desktop/static/app.js").read_text(encoding="utf-8")

    assert "function sessionActionMenu(session)" in source
    assert "function openRenameSession(session)" in source
    assert "function confirmDeleteSession()" in source
    assert "disabled: session.is_live" in source
    assert 'method: "DELETE"' in source


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
    assert bootstrap["bootstrap"]["sessions"] == {"sessions": [], "next_cursor": None}
    assert bootstrap["bootstrap"]["capabilities"] == {
        "history": True,
        "remote_title": True,
        "session_actions": True,
        "user_resume": True,
        "segment_history": True,
    }
    assert event == {"type": "warning", "message": "Session credits are running low."}
    assert "candidate" not in str([bootstrap, event])


def test_invalid_request_errors_do_not_echo_submitted_tokens(client: TestClient) -> None:
    response = client.post("/api/login", json={"token": {"secret": "candidate"}})

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request."}
    assert "candidate" not in response.text
