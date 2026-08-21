from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from broccoli_desktop.api import (
    Services,
    _audio_level_events,
    _RedactCapabilityKeyFilter,
    create_app,
    create_uvicorn_config,
)
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
from tests.visual_server import VISUAL_CAPABILITY_TOKEN, create_visual_app


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
    validates choices without also constructing a CaptureSession -- that path
    calls list_devices() on its own for reasons outside this fix's scope, which
    would make a start_session-based assertion about exact call counts couple
    this test to unrelated internals."""
    login(client)

    client.get("/api/devices")
    saved = client.put(
        "/api/devices/selection",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )

    assert saved.status_code == 204
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
