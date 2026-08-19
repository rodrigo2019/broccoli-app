from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient
from pytest import fixture

from broccoli_desktop.api import Services, create_app
from broccoli_desktop.config import RuntimeConfig
from broccoli_desktop.models import ConnectionState
from broccoli_desktop.runtime import DesktopRuntime, start_runtime
from tests.fakes import (
    VISUAL_TEST_BROCCOLI_URL,
    VISUAL_TEST_TOKEN,
    FakeCaptureBackend,
    visual_test_remote_factory,
)
from tests.visual_server import create_visual_app


@dataclass
class FakeCredentials:
    token: str | None = None

    def load_token(self) -> str | None:
        return self.token

    def save_token(self, token: str) -> None:
        self.token = token

    def delete_token(self) -> None:
        self.token = None


@dataclass
class FakeWindow:
    hidden: bool = False
    shown: bool = False
    restored: bool = False
    focused: bool = False
    destroyed: bool = False
    close_handler: Any = None

    def hide(self) -> None:
        self.hidden = True

    def show(self) -> None:
        self.shown = True

    def restore(self) -> None:
        self.restored = True

    def focus(self) -> None:
        self.focused = True

    def destroy(self) -> None:
        self.destroyed = True

    def bind_closing(self, handler: Any) -> None:
        self.close_handler = handler


@dataclass
class FakeSession:
    state: ConnectionState = ConnectionState.IDLE
    stop_calls: int = 0

    async def stop(self) -> None:
        self.stop_calls += 1
        self.state = ConnectionState.STOPPED


@dataclass
class FakeServer:
    controller: FakeSession | None = None
    healthy: bool = True
    started: bool = False
    shutdown_calls: int = 0
    url: str = "http://127.0.0.1:45678"

    def start(self) -> bool:
        self.started = True
        return self.healthy

    def run_coroutine(self, coroutine: Any) -> None:
        asyncio.run(coroutine)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


@dataclass
class FakeTray:
    running: bool = False
    stop_calls: int = 0

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.stop_calls += 1


@dataclass
class FakeDialog:
    answer: bool = True
    confirmations: int = 0
    errors: list[str] = field(default_factory=list)

    def confirm_quit(self) -> bool:
        self.confirmations += 1
        return self.answer

    def show_error(self, message: str) -> None:
        self.errors.append(message)


@fixture
def fake_window() -> FakeWindow:
    return FakeWindow()


@fixture
def fake_session() -> FakeSession:
    return FakeSession()


@fixture
def fake_server(fake_session: FakeSession) -> FakeServer:
    return FakeServer(controller=fake_session)


@fixture
def fake_tray() -> FakeTray:
    return FakeTray(running=True)


@fixture
def fake_dialog() -> FakeDialog:
    return FakeDialog()


@fixture
def runtime(
    fake_window: FakeWindow,
    fake_server: FakeServer,
    fake_tray: FakeTray,
    fake_dialog: FakeDialog,
) -> DesktopRuntime:
    return DesktopRuntime(
        server=fake_server,
        window=fake_window,
        tray=fake_tray,
        dialog=fake_dialog,
    )


def test_window_close_hides_instead_of_stopping_capture(
    fake_window: FakeWindow, fake_tray: FakeTray, runtime: DesktopRuntime
) -> None:
    """A close request must not stop the capture that the tray keeps alive."""
    runtime.on_window_closing()

    assert fake_window.hidden is True
    assert fake_tray.running is True
    assert runtime.session is not None
    assert runtime.session.stop_calls == 0


def test_quit_requires_confirmation_when_capture_is_active(
    fake_dialog: FakeDialog, runtime: DesktopRuntime
) -> None:
    """Declining the active-capture prompt must keep every runtime resource open."""
    assert runtime.session is not None
    runtime.session.state = ConnectionState.STREAMING
    fake_dialog.answer = False

    runtime.request_quit()

    assert runtime.window.destroyed is False
    assert fake_dialog.confirmations == 1
    assert runtime.session.stop_calls == 0


def test_non_streaming_quit_does_not_prompt_and_stops_runtime(
    fake_dialog: FakeDialog,
    fake_server: FakeServer,
    fake_tray: FakeTray,
    runtime: DesktopRuntime,
) -> None:
    """An idle runtime exits immediately because no recording can be interrupted."""
    runtime.request_quit()

    assert fake_dialog.confirmations == 0
    assert fake_server.shutdown_calls == 1
    assert fake_tray.stop_calls == 1
    assert runtime.window.destroyed is True


def test_shutdown_is_idempotent_for_active_capture(
    fake_server: FakeServer, fake_tray: FakeTray, runtime: DesktopRuntime
) -> None:
    """Repeated teardown requests must not leave duplicate capture or server shutdowns."""
    assert runtime.session is not None
    runtime.session.state = ConnectionState.STREAMING

    runtime.shutdown()
    runtime.shutdown()

    assert runtime.session.stop_calls == 1
    assert fake_server.shutdown_calls == 1
    assert fake_tray.stop_calls == 1
    assert runtime.window.destroyed is True


def test_server_start_failure_never_creates_a_window() -> None:
    """A failed health check must report locally and avoid opening the desktop UI."""
    server = FakeServer(healthy=False)
    dialog = FakeDialog()
    created_windows: list[FakeWindow] = []

    def create_window(_title: str, _url: str) -> FakeWindow:
        window = FakeWindow()
        created_windows.append(window)
        return window

    result = start_runtime(
        RuntimeConfig(environment="local", server_url="http://127.0.0.1:8000"),
        server_factory=lambda _config: server,
        window_factory=create_window,
        tray_factory=lambda _runtime: FakeTray(),
        dialog=dialog,
        webview_start=lambda: None,
    )

    assert result is None
    assert created_windows == []
    assert server.shutdown_calls == 1
    assert dialog.errors == ["Broccoli Desktop could not start its local service."]


def test_window_start_failure_stops_the_loopback_server() -> None:
    """A native-window failure must not leave the loopback worker running."""
    server = FakeServer()
    dialog = FakeDialog()

    def create_window(_title: str, _url: str) -> FakeWindow:
        raise RuntimeError("The native window could not be created.")

    result = start_runtime(
        RuntimeConfig(environment="local", server_url="http://127.0.0.1:8000"),
        server_factory=lambda _config: server,
        window_factory=create_window,
        tray_factory=lambda _runtime: FakeTray(),
        dialog=dialog,
        webview_start=lambda: None,
    )

    assert result is None
    assert server.shutdown_calls == 1
    assert dialog.errors == ["Broccoli Desktop could not open its window."]


def test_health_endpoint_is_local_and_dependency_free() -> None:
    """Runtime readiness must be checkable without touching credentials or a remote."""
    services = Services(
        credentials=FakeCredentials(),
        remote_factory=visual_test_remote_factory(),
        capture_backend=FakeCaptureBackend(),
        loopback_port=8765,
    )
    client = TestClient(create_app(services), headers={"host": "127.0.0.1:8765"})

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_visual_server_exposes_only_the_deterministic_browser_fixture() -> None:
    """Browser QA must use its fixed local remote, devices, and transcript seed only."""
    client = TestClient(create_visual_app(port=8765), headers={"host": "127.0.0.1:8765"})

    login = client.post("/api/login", json={"token": VISUAL_TEST_TOKEN})
    bootstrap = client.get("/api/bootstrap")

    assert login.status_code == 204
    assert bootstrap.json()["official_broccoli_url"] == VISUAL_TEST_BROCCOLI_URL
    assert bootstrap.json()["sessions"]["sessions"][0]["title"] == "Daily"
    assert VISUAL_TEST_TOKEN not in bootstrap.text
