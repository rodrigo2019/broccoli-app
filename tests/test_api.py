from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from broccoli_desktop.api import (
    Services,
    _audio_level_events,
    _default_proxy_prober,
    _RedactCapabilityKeyFilter,
    create_app,
    create_uvicorn_config,
)
from broccoli_desktop.credentials import CredentialStorageError, CredentialStore
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
    RealisticFakeKeyring,
    settle,
    visual_test_remote,
)
from tests.visual_server import VISUAL_CAPABILITY_TOKEN, create_visual_app


@dataclass
class FakeCredentials:
    token: str | None = None
    saved_tokens: list[str] = field(default_factory=list)
    deleted_count: int = 0
    unavailable: bool = False
    proxy_password: str | None = None
    saved_proxy_passwords: list[str] = field(default_factory=list)
    deleted_proxy_password_count: int = 0

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

    def load_proxy_password(self) -> str | None:
        if self.unavailable:
            raise CredentialStorageError("Credential storage is unavailable.")
        return self.proxy_password

    def save_proxy_password(self, password: str) -> None:
        if self.unavailable:
            raise CredentialStorageError("Credential storage is unavailable.")
        if not password:
            raise ValueError("A credential is required.")
        self.proxy_password = password
        self.saved_proxy_passwords.append(password)

    def delete_proxy_password(self) -> None:
        if self.unavailable:
            raise CredentialStorageError("Credential storage is unavailable.")
        self.proxy_password = None
        self.deleted_proxy_password_count += 1


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


@pytest.fixture
def tokened_client(services: Services) -> Iterator[TestClient]:
    services.capability_token = "launch-key"
    with TestClient(create_app(services), headers={"host": "127.0.0.1:8765"}) as client:
        yield client


def login(client: TestClient, *, key: str | None = None) -> None:
    headers = {"X-Broccoli-Key": key} if key else {}
    response = client.post("/api/login", json={"token": "test-token"}, headers=headers)
    assert response.status_code == 204


def start_capture(client: TestClient) -> None:
    response = client.post(
        "/api/sessions",
        json={"title": "Reunião", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    assert response.status_code == 201


def login_with_visual_token(client: TestClient) -> None:
    """Authenticate an in-process visual server with its fixed fake-only credential."""
    response = client.post(
        "/api/login",
        json={"token": VISUAL_TEST_TOKEN},
        headers={"X-Broccoli-Key": VISUAL_CAPABILITY_TOKEN},
    )

    assert response.status_code == 204


def bootstrap_payload(client: TestClient, *, key: str | None = None) -> dict:
    """Read the snapshot the event socket sends on connect.

    The bootstrap has one home now: opening the socket is how the UI learns
    whether it is authenticated, so the tests ask the same way the UI does.

    Closing straight after the first message is the shape a real client uses to
    reconnect, and it is what caught the gap between the handler speaking and
    the handler listening -- see ``_send_events``.
    """
    path = f"/api/events?k={key}" if key else "/api/events"
    with client.websocket_connect(path) as websocket:
        message = websocket.receive_json()
        websocket.close()

    assert message["type"] == "bootstrap"
    return message["bootstrap"]


def test_fake_bootstrap_exposes_the_same_device_contract() -> None:
    """The browser visual server must answer the same bootstrap the real one does."""
    client = TestClient(create_visual_app(port=8765), headers={"host": "127.0.0.1:8765"})
    login_with_visual_token(client)

    payload = bootstrap_payload(client, key=VISUAL_CAPABILITY_TOKEN)

    assert payload["authenticated"] is True
    assert [device["kind"] for device in payload["devices"]] == ["mic", "system"]


def test_visual_fake_serves_a_paginated_history_offline() -> None:
    """The browser check needs a page boundary to scroll past, and no network.

    It also needs both timestamp shapes the backend emits: a fake that only
    produced whole seconds would never catch the ordering bug the client had.
    """
    remote = visual_test_remote()

    first = asyncio.run(remote.list_sessions(None, ""))
    second = asyncio.run(remote.list_sessions(first.next_cursor, ""))
    third = asyncio.run(remote.list_sessions(second.next_cursor, ""))

    assert len(first.sessions) == 20
    assert first.next_cursor == "20"
    assert len(second.sessions) == 20
    assert second.next_cursor == "40"
    assert third.sessions
    assert third.next_cursor is None
    assert any("." in (session.started_at or "") for session in first.sessions)
    assert any("." not in (session.started_at or "") for session in first.sessions)


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
    assert 'data-testid="open-broccoli"' not in response.text
    assert 'data-testid="rename-session"' in response.text
    assert 'data-testid="jump-to-latest"' in response.text
    assert 'id="themeToggleButton"' not in response.text
    assert 'id="renameSessionModal"' in response.text
    assert 'id="deleteSessionModal"' in response.text
    assert 'data-testid="notification-stack"' in response.text
    assert 'data-testid="status-banner"' not in response.text
    assert 'data-testid="sessions-sentinel"' in response.text
    assert 'data-testid="load-more"' not in response.text
    assert 'data-testid="transcript-timeline"' in response.text
    # The token field's accessible name must come from its visible Portuguese
    # <legend>, not an overriding English aria-label (WCAG 2.5.3 Label in
    # Name). A bare <legend> does not, by itself, name a sibling <input> (this
    # app's own rename-session field uses the same fieldset/legend shape and
    # gets its name from `placeholder` alone), so aria-labelledby points the
    # field at the legend's own id explicitly. Its helper text is wired in via
    # aria-describedby so a screen reader announces it too.
    assert 'aria-label="Broccoli access token"' not in response.text
    assert 'id="tokenInputLegend"' in response.text
    assert 'aria-labelledby="tokenInputLegend"' in response.text
    assert 'aria-describedby="tokenInputHelp"' in response.text
    assert 'id="tokenInputHelp"' in response.text
    # The proxy fields carry the same fieldset/legend shape as the token field
    # above, so they need the same explicit wiring -- without it their
    # accessible names fall through to `placeholder` and they announce as
    # "proxy.broccoli.local", "8080", "usuario-local" and "Deixe em branco para
    # manter a senha salva" instead of Endereço, Porta, Usuário and Senha.
    for field_id in ("proxyHost", "proxyPort", "proxyUsername", "proxyPassword"):
        assert f'id="{field_id}Legend"' in response.text
        assert f'aria-labelledby="{field_id}Legend"' in response.text
    assert 'aria-describedby="proxyPasswordHelp"' in response.text
    assert 'id="proxyPasswordHelp"' in response.text
    # The login screen needs its own way into the settings panel: /api/settings
    # is unauthenticated precisely so a proxy can be configured before the login
    # request can reach the backend, and #settingsButton lives in the sidebar
    # footer, which stays hidden until authentication.
    assert 'data-testid="network-settings"' in response.text
    assert 'id="settingsDevicesSection"' in response.text
    assert 'id="settingsAppearanceSection"' in response.text
    assert 'data-testid="session-search"' in response.text
    assert 'id="drawer-toggle"' in response.text
    assert 'data-testid="capture-indicator"' in response.text
    assert "vendor/bootstrap-icons/bootstrap-icons.css" in response.text
    assert 'class="form-control' not in response.text
    assert "label-text" not in response.text
    assert "input-bordered" not in response.text
    assert "select-bordered" not in response.text
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


def test_bootstrap_carries_the_devices_the_first_screen_needs(
    client: TestClient,
    fake_credentials: FakeCredentials,
) -> None:
    login(client)

    payload = bootstrap_payload(client)

    assert payload == {
        "authenticated": True,
        "devices": [
            {"device_id": "mic-1", "label": "Microphone One", "kind": "mic"},
            {"device_id": "system-1", "label": "Speakers", "kind": "system"},
        ],
        "selected_devices": None,
        "state": "idle",
        "session": None,
    }
    assert "test-token" not in str(payload)
    assert fake_credentials.token == "test-token"


def test_bootstrap_is_unauthenticated_without_a_stored_credential(client: TestClient) -> None:
    """The socket answers instead of refusing, so the login screen has a payload."""
    payload = bootstrap_payload(client)

    assert payload == {
        "authenticated": False,
        "devices": [
            {"device_id": "mic-1", "label": "Microphone One", "kind": "mic"},
            {"device_id": "system-1", "label": "Speakers", "kind": "system"},
        ],
        "selected_devices": None,
        "state": "idle",
        "session": None,
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


def test_device_enumeration_is_cached_between_calls(
    client: TestClient, fake_capture: FakeCaptureBackend
) -> None:
    """Windows device enumeration routinely takes hundreds of milliseconds, and
    it ran on every /api/devices, every bootstrap and every session start --
    each one freezing the event socket and the meter stream with it."""
    login(client)

    client.get("/api/devices")
    client.get("/api/devices")

    assert fake_capture.list_devices_calls == 1


def test_device_selection_reuses_the_cached_device_list(
    client: TestClient, fake_capture: FakeCaptureBackend
) -> None:
    """_validated_choices used to call backend.list_devices() directly, bypassing
    the cache and blocking the loop again on every save, audio-level check,
    session start and resume. Exercised here through device selection, which
    validates choices without also constructing a CaptureSession; the session
    start path is covered separately below."""
    login(client)

    client.get("/api/devices")
    saved = client.put(
        "/api/devices/selection",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )

    assert saved.status_code == 204
    assert fake_capture.list_devices_calls == 1


def test_starting_a_session_reuses_the_cached_device_list(
    client: TestClient, fake_capture: FakeCaptureBackend
) -> None:
    """Starting a session used to enumerate three more times on top of the
    cached validation read: once to resolve the system device's label, once in
    CaptureSession's constructor -- both on the event loop -- and once more
    inside CaptureSession.start(). That is the hot path the TTL cache was
    written to protect, and it was the one path that bypassed it."""
    login(client)
    client.get("/api/devices")

    start_capture(client)

    assert fake_capture.list_devices_calls == 1


def test_bootstrap_reuses_the_cached_device_list_when_restoring_a_selection(
    fake_credentials: FakeCredentials,
    fake_remote_factory: FakeRemoteFactory,
    fake_capture: FakeCaptureBackend,
) -> None:
    """_choices_are_available used to call backend.list_devices() directly from
    Services._create_controller, bypassing the cache on every token change."""
    settings = FakeDeviceSettings(selection=CaptureChoices("mic-1", "system-1"))
    services = Services(
        credentials=fake_credentials,
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
        device_settings=settings,
    )
    fake_credentials.save_token("candidate")
    client = TestClient(create_app(services), headers={"host": "127.0.0.1"})

    client.get("/api/devices")
    bootstrap_payload(client)

    assert fake_capture.list_devices_calls == 1


def test_audio_level_stream_requires_a_credential(client: TestClient) -> None:
    assert client.get("/api/audio-levels/stream").status_code == 401
    assert (
        client.get(
            "/api/audio-levels/stream",
            params={"microphone_id": "mic-1", "system_device_id": "system-1"},
        ).status_code
        == 401
    )


def test_audio_level_stream_rejects_a_device_check_that_is_not_available(
    client: TestClient,
) -> None:
    login(client)

    response = client.get(
        "/api/audio-levels/stream",
        params={"microphone_id": "mic-1", "system_device_id": "mic-1"},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Select an available microphone and system device."}


@pytest.mark.asyncio
async def test_audio_level_stream_sends_initial_and_live_snapshots(
    client: TestClient,
    services: Services,
    fake_capture: FakeCaptureBackend,
) -> None:
    login(client)
    services.start_audio_level_monitor(CaptureChoices("mic-1", "system-1"))

    events = _audio_level_events(services, None)
    initial = await anext(events)
    assert initial.startswith("data: {")
    assert '"active":true' in initial

    fake_capture.handles["mic-1"].emit(b"\xff\x7f")
    update = await asyncio.wait_for(anext(events), timeout=1)
    assert update.startswith("data: {")
    assert '"active":true' in update
    assert b"\xff\x7f" not in update.encode("utf-8")
    await events.aclose()
    services.stop_audio_level_monitor()


@pytest.mark.asyncio
async def test_audio_level_device_check_lives_and_dies_with_its_stream(
    client: TestClient,
    services: Services,
    fake_capture: FakeCaptureBackend,
) -> None:
    """Closing the stream releases the microphone, so a lost window cannot hold it."""
    login(client)

    events = _audio_level_events(services, CaptureChoices("mic-1", "system-1"))
    initial = await anext(events)

    assert '"active":true' in initial
    assert fake_capture.closed_sources == set()

    fake_capture.handles["mic-1"].emit(b"\xff\x7f")
    update = await asyncio.wait_for(anext(events), timeout=1)
    assert json.loads(update.removeprefix("data: "))["microphone"]["level"] > 0.99

    await events.aclose()

    assert fake_capture.closed_sources == {"mic-1", "system-1"}


@pytest.mark.asyncio
async def test_audio_level_stream_reports_an_unavailable_device_without_a_snapshot(
    client: TestClient,
    services: Services,
    fake_capture: FakeCaptureBackend,
) -> None:
    login(client)
    fake_capture.fail_opening = "mic-1"

    events = _audio_level_events(services, CaptureChoices("mic-1", "system-1"))
    first = await anext(events)

    assert first.startswith("event: device_error\n")
    with pytest.raises(StopAsyncIteration):
        await anext(events)


@pytest.mark.asyncio
async def test_audio_level_device_check_leaves_a_live_capture_meter_alone(
    client: TestClient,
    services: Services,
) -> None:
    """A test stream that closes after the capture took over must not blank the footer."""
    login(client)

    events = _audio_level_events(services, CaptureChoices("mic-1", "system-1"))
    await anext(events)
    services.audio_levels.set_capture_active(True)
    await events.aclose()

    assert services.audio_levels.capture_active is True
    assert services.audio_levels.snapshot().active is True


def test_audio_level_check_is_refused_while_a_capture_owns_the_devices(
    client: TestClient,
) -> None:
    login(client)
    capture = client.post(
        "/api/sessions",
        json={"title": "", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    refused = client.get(
        "/api/audio-levels/stream",
        params={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )

    assert capture.status_code == 201
    assert refused.status_code == 409

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
    controller, _remote = await services.authenticated()  # type: ignore[misc]
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")
    fake_remote_factory.remote.revoke_token()

    await fake_remote_factory.remote.emit_failure()
    # settle(), not two bare turns: the recovery path's capture teardown hops
    # to a worker thread, and yielding to the loop does not wait for a thread.
    await settle()

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
    bootstrap = bootstrap_payload(fresh_client)

    assert started.status_code == 201
    assert stopped.status_code == 204
    assert settings.saved == [CaptureChoices("mic-1", "system-1")]
    assert bootstrap["selected_devices"] == {
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
    reopened = bootstrap_payload(reopened_client)
    cleared = client.delete("/api/devices/selection")

    assert saved.status_code == 204
    assert settings.saved == [CaptureChoices("mic-1", "system-1")]
    assert reopened["selected_devices"] == {
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

    bootstrap = bootstrap_payload(client)
    start = client.post(
        "/api/sessions",
        json={"title": "", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )

    assert bootstrap["selected_devices"] is None
    assert settings.selection is None
    assert settings.clear_count == 1
    assert start.status_code == 422


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

    response = client.get("/api/sessions")

    assert response.status_code == 503
    assert response.json() == {"detail": "Credential storage is unavailable."}


def test_host_header_must_name_the_local_loopback_service(client: TestClient) -> None:
    allowed = client.get("/health", headers={"host": "LOCALHOST:8765"})
    response = client.get("/health", headers={"host": "example.invalid"})

    assert allowed.status_code == 200
    assert response.status_code == 400
    assert response.json() == {"detail": "Local host required."}


def test_a_foreign_origin_cannot_open_the_event_socket(client: TestClient) -> None:
    """A page on any other origin can reach ws://127.0.0.1:PORT because
    WebSockets are not subject to the same-origin policy. Only the Origin
    header separates the app's own window from that page."""
    login(client)

    with pytest.raises(WebSocketDisconnect) as rejection:
        with client.websocket_connect(
            "/api/events", headers={"origin": "http://evil.example"}
        ) as websocket:
            websocket.receive_json()

    assert rejection.value.code == 1008


def test_the_window_origin_still_opens_the_event_socket(client: TestClient) -> None:
    login(client)

    with client.websocket_connect(
        "/api/events", headers={"origin": "http://127.0.0.1:8765"}
    ) as websocket:
        assert websocket.receive_json()["type"] == "bootstrap"


def test_a_foreign_origin_cannot_reach_the_http_api(client: TestClient) -> None:
    login(client)

    response = client.get("/api/devices", headers={"origin": "http://evil.example"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Local origin required."}


def test_a_request_without_an_origin_is_allowed(client: TestClient) -> None:
    """Non-browser local callers -- the app's own httpx, a developer's curl --
    send no Origin at all."""
    login(client)

    assert client.get("/api/devices").status_code == 200


def test_the_api_requires_the_capability_token(tokened_client: TestClient) -> None:
    """Origin stops a web page. It does not stop another process on the machine,
    which sends no Origin at all -- that is what this token is for."""
    login(tokened_client, key="launch-key")

    without = tokened_client.get("/api/devices")
    with_key = tokened_client.get("/api/devices", headers={"X-Broccoli-Key": "launch-key"})

    assert without.status_code == 403
    assert without.json() == {"detail": "Local key required."}
    assert with_key.status_code == 200


def test_the_api_accepts_the_capability_token_as_a_query_parameter(
    tokened_client: TestClient,
) -> None:
    """EventSource cannot set headers either, so the audio-level stream and any
    other request the UI cannot attach a header to falls back to `?k=`."""
    login(tokened_client, key="launch-key")

    response = tokened_client.get("/api/devices?k=launch-key")

    assert response.status_code == 200


def test_the_shell_and_its_assets_do_not_require_the_token(
    tokened_client: TestClient,
) -> None:
    """The page has to boot before it can present a key."""
    assert tokened_client.get("/").status_code == 200
    assert tokened_client.get("/static/app.js").status_code == 200


def test_a_non_ascii_capability_key_is_rejected_rather_than_crashing(
    tokened_client: TestClient,
) -> None:
    """secrets.compare_digest raises TypeError on a str carrying anything above
    U+007F, and headers are decoded latin-1 -- so a key with a single accented
    byte in it turned a 403 into an unhandled 500, telling a caller its garbage
    key was different in kind from an ordinary wrong one."""
    # Raw bytes, the way a real caller sends them: the ASGI server decodes
    # headers latin-1, so this arrives as a str with a character above U+007F.
    response = tokened_client.get(
        "/api/devices", headers={"X-Broccoli-Key": "chave-inválida".encode("latin-1")}
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Local key required."}


def test_the_window_can_recover_its_launch_key_after_a_reload(
    tokened_client: TestClient,
) -> None:
    """A reload of the window used to brick it.

    The launch key arrives once in the URL and is stripped with
    history.replaceState. WebView2 enables F5/Ctrl-R by default, a renderer
    crash reloads, and the context menu offers it -- and the reloaded page had
    no key anywhere, so every /api/* answered 403 and the event socket closed
    with 1008. No bootstrap ever arrived, state.authenticated stayed false, and
    the socket's close handler bailed without retrying: a window that rendered
    normally and could not do anything, with nothing on screen saying so.

    sessionStorage is the recovery, and it is the right scope: it is per window
    and dropped when the window closes, so the key still lives exactly one
    launch, unlike localStorage which would hand a stale key to the next one.
    The behavioural check -- a real reload with no key in the URL -- lives in
    scripts/visual-check.ps1; this pins the mechanism against a silent revert.
    """
    source = tokened_client.get("/static/app.js").text

    assert '"broccoli-desktop-launch-key"' in source
    assert "sessionStorage.setItem(CAPABILITY_STORAGE_KEY" in source
    assert "sessionStorage.getItem(CAPABILITY_STORAGE_KEY)" in source
    # Still stripped from the address bar -- recovery, not a rollback.
    assert 'window.history.replaceState(null, "", clean)' in source


def test_the_event_socket_requires_the_capability_token(
    tokened_client: TestClient,
) -> None:
    login(tokened_client, key="launch-key")

    with pytest.raises(WebSocketDisconnect) as rejection:
        with tokened_client.websocket_connect("/api/events") as websocket:
            websocket.receive_json()

    assert rejection.value.code == 1008

    with tokened_client.websocket_connect("/api/events?k=launch-key") as websocket:
        assert websocket.receive_json()["type"] == "bootstrap"


def test_creating_the_uvicorn_config_installs_capability_key_log_redaction(
    services: Services,
) -> None:
    """uvicorn logs the raw request URL in two places uvicorn's own
    `access_log` flag does not fully cover: the HTTP access log
    (`uvicorn.access`) and, independently, the WebSocket handshake log
    (`uvicorn.error`) -- both need the filter, or the launch key that rides
    in `?k=` reaches a log regardless."""
    create_uvicorn_config(create_app(services), port=8765)

    for logger_name in ("uvicorn.access", "uvicorn.error"):
        filters = logging.getLogger(logger_name).filters
        assert any(isinstance(installed, _RedactCapabilityKeyFilter) for installed in filters)


def test_the_capability_key_redaction_filter_strips_a_logged_request_line() -> None:
    """Directly exercises the filter uvicorn's own log records pass through --
    once shaped like its HTTP access log line, once like its WebSocket
    handshake line -- since neither goes anywhere near our own middleware or
    a TestClient (both are logged by uvicorn's protocol implementations,
    which only run against a real socket)."""
    token = "super-secret-launch-key"
    redact = _RedactCapabilityKeyFilter()

    http_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "GET", f"/?k={token}", "1.1", 200),
        exc_info=None,
    )
    websocket_record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='%s - "WebSocket %s" [accepted]',
        args=("127.0.0.1:54321", f"/api/events?k={token}"),
        exc_info=None,
    )

    assert redact.filter(http_record) is True
    assert redact.filter(websocket_record) is True

    http_message = http_record.getMessage()
    websocket_message = websocket_record.getMessage()

    assert token not in http_message
    assert token not in websocket_message
    # The rest of the line -- what actually helps someone debugging a report --
    # survives the redaction untouched.
    assert http_message == '127.0.0.1:54321 - "GET /?k=REDACTED HTTP/1.1" 200'
    assert websocket_message == '127.0.0.1:54321 - "WebSocket /api/events?k=REDACTED" [accepted]'


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
    assert event == {"type": "warning", "message": "Session credits are running low."}
    assert "candidate" not in str([bootstrap, event])


def test_invalid_request_errors_do_not_echo_submitted_tokens(client: TestClient) -> None:
    response = client.post("/api/login", json={"token": {"secret": "candidate"}})

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request."}
    assert "candidate" not in response.text


def test_session_search_reaches_the_remote_with_the_typed_term(
    client: TestClient,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    """The sidebar's search box is the only reader of the `q` parameter."""
    login(client)
    fake_remote_factory.remote.sessions = {
        "session-1": SessionSummary(
            uuid_code="session-1",
            title="Weekly sync",
            status="ended",
            started_at="2026-08-19T10:00:00Z",
            ended_at="2026-08-19T11:00:00Z",
            device_label="Speakers",
            segment_count=0,
            is_live=False,
        ),
        "session-2": SessionSummary(
            uuid_code="session-2",
            title="Design review",
            status="ended",
            started_at="2026-08-19T12:00:00Z",
            ended_at="2026-08-19T13:00:00Z",
            device_label="Speakers",
            segment_count=0,
            is_live=False,
        ),
    }

    matched = client.get("/api/sessions", params={"q": "design"})
    missed = client.get("/api/sessions", params={"q": "zzz-no-match"})

    assert [session["uuid_code"] for session in matched.json()["sessions"]] == ["session-2"]
    assert missed.json() == {"sessions": [], "next_cursor": None}


def test_the_event_socket_listens_before_it_speaks(client: TestClient) -> None:
    """A client that answers the bootstrap by closing must not race the handler.

    Reconnecting is exactly that shape: read the snapshot, drop the socket. If
    the bootstrap went out before the receive task existed, the disconnect could
    land with nobody reading it and the handler would be torn down mid-flight.
    """
    login(client)

    for _ in range(20):
        with client.websocket_connect("/api/events") as websocket:
            assert websocket.receive_json()["type"] == "bootstrap"
            websocket.close()


def test_session_name_suggests_a_title_before_the_session_exists(client: TestClient) -> None:
    """The draft is named first, so the title the user sees is the one persisted."""
    unauthenticated = client.get("/api/session-name")
    login(client)

    first = client.get("/api/session-name")
    second = client.get("/api/session-name")

    assert unauthenticated.status_code == 401
    assert first.status_code == 200
    assert first.json()["title"]
    assert first.json()["title"] != second.json()["title"]


def test_a_capture_started_without_a_title_is_named_by_the_server(
    client: TestClient,
    fake_remote_factory: FakeRemoteFactory,
) -> None:
    """A failed suggestion request must not be what leaves a meeting unnamed."""
    login(client)

    response = client.post(
        "/api/sessions",
        json={"title": "", "microphone_id": "mic-1", "system_device_id": "system-1"},
    )

    assert response.status_code == 201
    assert response.json()["title"]
    assert fake_remote_factory.remote.sessions["session-1"].title
    client.post("/api/sessions/stop")


def test_a_supplied_title_is_never_replaced_by_a_generated_one(client: TestClient) -> None:
    login(client)

    response = client.post(
        "/api/sessions",
        json={
            "title": "Retrospectiva da sprint",
            "microphone_id": "mic-1",
            "system_device_id": "system-1",
        },
    )

    assert response.json()["title"] == "Retrospectiva da sprint"
    client.post("/api/sessions/stop")


def test_login_during_a_capture_is_refused(client: TestClient, services: Services) -> None:
    """set_authenticated replaces the controller outright. The old one keeps two
    open WASAPI streams and a remote socket with nothing left holding a
    reference that can stop them -- the microphone stays open until the process
    exits."""
    login(client)
    start_capture(client)
    original = services.controller

    response = client.post("/api/login", json={"token": "another-token"})

    assert response.status_code == 409
    assert response.json() == {"detail": "A capture is active."}
    assert services.controller is original


# --------------------------------------------------------------------- proxy settings


@dataclass
class FakeProxyProber:
    """Deterministic stand-in for the real network probe, so these tests never
    reach an actual proxy or backend."""

    result: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, target_url: str, proxy_url: str) -> bool:
        self.calls.append((target_url, proxy_url))
        return self.result


def test_the_proxy_password_never_reaches_the_settings_payload(
    client: TestClient, services: Services
) -> None:
    """The panel used to persist the password to localStorage in clear text, in
    a product whose selling point is that credentials live in the Windows
    vault."""
    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "secret",
            }
        },
    )

    stored = client.get("/api/settings").json()

    assert "secret" not in json.dumps(stored)
    assert services.credentials.load_proxy_password() == "secret"


def test_clearing_the_proxy_deletes_the_stored_password(
    client: TestClient, services: Services
) -> None:
    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "p",
                "port": 1,
                "username": "u",
                "password": "secret",
            }
        },
    )

    client.post("/api/settings", json={"proxy": {"enabled": False}})

    assert services.credentials.load_proxy_password() is None


def test_saved_proxy_settings_are_returned_without_a_password_field(client: TestClient) -> None:
    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "secret",
            }
        },
    )

    stored = client.get("/api/settings").json()

    assert stored == {
        "proxy": {"enabled": True, "host": "proxy.local", "port": 8080, "username": "user"}
    }
    assert "password" not in json.dumps(stored)


def test_settings_default_to_a_disabled_proxy(client: TestClient) -> None:
    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json() == {"proxy": {"enabled": False, "host": "", "port": 0, "username": ""}}


def test_settings_are_reachable_without_authentication(client: TestClient) -> None:
    """The proxy may be exactly what a corporate network needs to reach the
    login endpoint in the first place, so it has to be configurable pre-login."""
    response = client.get("/api/settings")

    assert response.status_code == 200


def test_enabling_the_proxy_without_a_host_or_port_is_rejected(
    client: TestClient, services: Services
) -> None:
    response = client.post("/api/settings", json={"proxy": {"enabled": True}})

    assert response.status_code == 422
    # English, machine-readable, matching every other ApiError detail in this
    # file -- app.js's API_MESSAGES table maps English keys to pt-BR text, and
    # a detail written in Portuguese here would fall through untranslated to
    # the generic fallback toast, making this specific, actionable message
    # unreachable through the UI.
    assert response.json() == {"detail": "Proxy host and port are required."}
    assert services.credentials.load_proxy_password() is None


def test_resaving_proxy_settings_without_a_password_leaves_the_stored_one_untouched(
    client: TestClient, services: Services
) -> None:
    """The saved password never comes back to the page, so editing the host or
    port later must not force the user to retype it."""
    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "secret",
            }
        },
    )

    response = client.post(
        "/api/settings",
        json={"proxy": {"enabled": True, "host": "proxy.local", "port": 9090, "username": "user"}},
    )

    assert response.status_code == 204
    assert services.credentials.load_proxy_password() == "secret"
    assert client.get("/api/settings").json()["proxy"]["port"] == 9090


def test_a_proxy_saved_after_login_reaches_the_next_remote(
    fake_credentials: FakeCredentials, fake_capture: FakeCaptureBackend
) -> None:
    """The panel used to be reachable and inert.

    remote_factory reads proxy_url() at the moment it runs, and authenticated()
    only reran it when the *token* changed -- so a proxy saved after login was
    never used until sign-out or a restart, while "Testar conexão" reported
    success, actively telling the user it worked. The factory here mirrors
    production's (runtime.py's create_services) by reading services.proxy_url()
    inside the closure, which is the exact shape the defect lived in.
    """
    built_with: list[str | None] = []
    services: Services

    def remote_factory(_token: str) -> FakeSessionRemote:
        built_with.append(services.proxy_url())
        return FakeSessionRemote()

    services = Services(
        credentials=fake_credentials,
        remote_factory=remote_factory,
        capture_backend=fake_capture,
        loopback_port=8765,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1:8765"})
    login(client)
    controller_before = services.controller
    assert built_with == [None]

    save = client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "secret",
            }
        },
    )
    assert save.status_code == 204

    assert client.get("/api/session-name").status_code == 200

    assert built_with[-1] == "http://user:secret@proxy.local:8080"
    # The controller has to survive the swap: every open /api/events socket is
    # subscribed to *this* object's EventHub, and replacing it would leave the
    # window's feed subscribed to something nothing publishes to any more.
    assert services.controller is controller_before


def test_clearing_the_proxy_after_login_reaches_the_next_remote(
    fake_credentials: FakeCredentials, fake_capture: FakeCaptureBackend
) -> None:
    """Same defect in the other direction: turning a proxy off has to stop
    routing through it without waiting for a restart."""
    built_with: list[str | None] = []
    services: Services

    def remote_factory(_token: str) -> FakeSessionRemote:
        built_with.append(services.proxy_url())
        return FakeSessionRemote()

    services = Services(
        credentials=fake_credentials,
        remote_factory=remote_factory,
        capture_backend=fake_capture,
        loopback_port=8765,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1:8765"})
    client.post(
        "/api/settings",
        json={"proxy": {"enabled": True, "host": "proxy.local", "port": 8080, "password": "s"}},
    )
    login(client)
    assert built_with == ["http://proxy.local:8080"]

    assert client.post("/api/settings", json={"proxy": {"enabled": False}}).status_code == 204
    assert client.get("/api/session-name").status_code == 200

    assert built_with[-1] is None


def test_a_proxy_change_is_refused_while_a_capture_is_running(
    client: TestClient, services: Services
) -> None:
    """Saving rebuilds the transport. A live run holds a stream opened through
    the old one, so the change is refused rather than applied underneath it --
    the same answer an audio-device change gives."""
    login(client)
    start_capture(client)

    response = client.post(
        "/api/settings",
        json={"proxy": {"enabled": True, "host": "proxy.local", "port": 8080}},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Stop the active capture before changing the proxy."}


def test_saving_a_new_password_replaces_the_stored_one(
    client: TestClient, services: Services
) -> None:
    client.post(
        "/api/settings",
        json={
            "proxy": {"enabled": True, "host": "p", "port": 1, "username": "u", "password": "first"}
        },
    )

    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "p",
                "port": 1,
                "username": "u",
                "password": "second",
            }
        },
    )

    assert services.credentials.load_proxy_password() == "second"


def test_proxy_url_is_none_when_disabled(services: Services) -> None:
    assert services.proxy_url() is None


def test_proxy_url_is_built_from_saved_settings_and_the_vault_password(
    client: TestClient, services: Services
) -> None:
    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "secret",
            }
        },
    )

    assert services.proxy_url() == "http://user:secret@proxy.local:8080"


def test_proxy_url_omits_credentials_when_no_username_is_configured(
    client: TestClient, services: Services
) -> None:
    client.post(
        "/api/settings",
        json={"proxy": {"enabled": True, "host": "proxy.local", "port": 8080, "username": ""}},
    )

    assert services.proxy_url() == "http://proxy.local:8080"


def test_proxy_url_fails_closed_when_a_username_is_configured_but_no_password_is_stored(
    client: TestClient, services: Services
) -> None:
    """An enabled proxy config with a username but no vault password behind it
    is an inconsistent state -- a local settings write that landed without its
    matching vault write, or the vault entry removed out from under this
    process -- never "this proxy needs no password". proxy_url() must refuse
    rather than silently building an unauthenticated http://user@host:port."""
    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "secret",
            }
        },
    )
    services.credentials.delete_proxy_password()

    assert services.proxy_url() is None


def test_test_proxy_reports_success(client: TestClient, services: Services) -> None:
    prober = FakeProxyProber(result=True)
    services.proxy_prober = prober
    services.backend_url = "https://backend.example"

    response = client.post(
        "/api/settings/test-proxy",
        json={"host": "proxy.local", "port": 8080, "username": "user", "password": "secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert prober.calls == [("https://backend.example", "http://user:secret@proxy.local:8080")]


def test_test_proxy_reports_failure_identically_for_a_bad_password_and_an_unreachable_host(
    client: TestClient, services: Services
) -> None:
    """The response must not let a caller tell a wrong password apart from a host
    that never answers -- either would otherwise turn this into a credential
    oracle for whoever can reach the loopback API."""
    services.backend_url = "https://backend.example"

    services.proxy_prober = FakeProxyProber(result=False)
    wrong_password_response = client.post(
        "/api/settings/test-proxy",
        json={"host": "proxy.local", "port": 8080, "username": "user", "password": "wrong"},
    )

    services.proxy_prober = FakeProxyProber(result=False)
    unreachable_host_response = client.post(
        "/api/settings/test-proxy",
        json={
            "host": "unreachable.invalid",
            "port": 8080,
            "username": "user",
            "password": "secret",
        },
    )

    assert wrong_password_response.status_code == unreachable_host_response.status_code == 200
    assert wrong_password_response.json() == unreachable_host_response.json() == {"ok": False}


def test_test_proxy_never_echoes_the_password_back(client: TestClient, services: Services) -> None:
    services.backend_url = "https://backend.example"
    services.proxy_prober = FakeProxyProber(result=False)

    response = client.post(
        "/api/settings/test-proxy",
        json={
            "host": "proxy.local",
            "port": 8080,
            "username": "user",
            "password": "super-secret-value",
        },
    )

    assert "super-secret-value" not in response.text


def test_test_proxy_requires_a_host_and_port(client: TestClient) -> None:
    response = client.post("/api/settings/test-proxy", json={"host": "", "port": 0})

    assert response.status_code == 422
    assert response.json() == {"detail": "Proxy host and port are required."}


def test_test_proxy_checks_the_submitted_values_not_the_saved_ones(
    client: TestClient, services: Services
) -> None:
    """The button tests whatever is currently in the form, not what is already
    saved -- the point is to validate a configuration before committing to it."""
    services.backend_url = "https://backend.example"
    services.proxy_prober = FakeProxyProber(result=True)

    response = client.post(
        "/api/settings/test-proxy",
        json={"host": "proxy.local", "port": 8080, "username": "", "password": ""},
    )

    assert response.json() == {"ok": True}


def test_saving_and_testing_the_proxy_never_logs_the_password(
    client: TestClient, services: Services, caplog: pytest.LogCaptureFixture
) -> None:
    services.backend_url = "https://backend.example"
    services.proxy_prober = FakeProxyProber(result=True)
    caplog.set_level(logging.DEBUG)

    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "log-sentinel-secret",
            }
        },
    )
    client.post(
        "/api/settings/test-proxy",
        json={
            "host": "proxy.local",
            "port": 8080,
            "username": "user",
            "password": "log-sentinel-secret",
        },
    )

    assert "log-sentinel-secret" not in caplog.text


def test_a_failed_vault_write_does_not_leave_the_proxy_enabled_locally(
    client: TestClient, services: Services, fake_credentials: FakeCredentials
) -> None:
    """The password is saved to the vault before the connection settings are
    marked enabled -- otherwise a vault write that fails partway through
    would leave an "enabled" proxy on disk with no password behind it, and
    proxy_url() would silently fall back to an unauthenticated connection."""
    fake_credentials.unavailable = True

    response = client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "secret",
            }
        },
    )

    assert response.status_code == 503
    assert services.proxy_settings.load().enabled is False


def test_a_failed_vault_delete_does_not_leave_the_proxy_disabled_locally(
    client: TestClient, services: Services, fake_credentials: FakeCredentials
) -> None:
    """Mirrors the save-side ordering fix: the vault password is deleted
    before the connection settings are marked disabled, so a vault failure
    here leaves the proxy exactly as it was rather than showing "disabled"
    locally while the password it was supposed to take with it lingers."""
    client.post(
        "/api/settings",
        json={
            "proxy": {
                "enabled": True,
                "host": "proxy.local",
                "port": 8080,
                "username": "user",
                "password": "secret",
            }
        },
    )
    fake_credentials.unavailable = True

    response = client.post("/api/settings", json={"proxy": {"enabled": False}})

    assert response.status_code == 503
    assert services.proxy_settings.load().enabled is True


def test_saving_disabled_settings_succeeds_when_no_proxy_password_was_ever_stored(
    fake_capture: FakeCaptureBackend, fake_remote_factory: FakeRemoteFactory
) -> None:
    """Reproduces the real-world failure directly: on Windows,
    keyring.delete_password raises PasswordDeleteError when nothing matches,
    and {"enabled": false} -- the ordinary Save for everyone who never
    configured a proxy -- used to call delete_proxy_password() unconditionally.
    FakeCredentials (used by the `client`/`services` fixtures everywhere else
    in this file) is lenient exactly where the real backend is strict, so this
    test wires the real CredentialStore against a keyring fake that models
    Windows' actual behaviour instead.
    """
    services = Services(
        credentials=CredentialStore(RealisticFakeKeyring()),
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1"})

    response = client.post("/api/settings", json={"proxy": {"enabled": False}})

    assert response.status_code == 204


def test_logout_succeeds_when_no_token_was_ever_stored(
    fake_capture: FakeCaptureBackend, fake_remote_factory: FakeRemoteFactory
) -> None:
    """The same delete-on-missing hazard applies to the token: a double logout,
    or a logout after the token was already cleared some other way, must not
    503 just because there was nothing left to delete."""
    services = Services(
        credentials=CredentialStore(RealisticFakeKeyring()),
        remote_factory=fake_remote_factory,
        capture_backend=fake_capture,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1"})

    response = client.delete("/api/login")

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_default_proxy_prober_treats_a_407_response_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong proxy password on a plain-HTTP target comes back as an ordinary
    407 response rather than a raised exception -- httpx only raises for a
    rejected HTTPS CONNECT tunnel, not for a proxy relaying its own rejection
    on a forwarded HTTP request. Mapping that 407 to the same False an
    unreachable host already produces adds no new distinguishing signal --
    it only corrects the false positive."""

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        return httpx.Response(407, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await _default_proxy_prober(
        "http://backend.example", "http://user:wrong@proxy.local:8080"
    )

    assert result is False


@pytest.mark.asyncio
async def test_default_proxy_prober_succeeds_for_a_non_407_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await _default_proxy_prober(
        "http://backend.example", "http://user:pass@proxy.local:8080"
    )

    assert result is True
