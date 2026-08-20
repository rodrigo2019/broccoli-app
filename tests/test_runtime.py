from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from fastapi.testclient import TestClient
from pytest import fixture, raises

from broccoli_desktop.api import Services, create_app
from broccoli_desktop.config import RuntimeConfig
from broccoli_desktop.models import ConnectionState
from broccoli_desktop.runtime import (
    DesktopRuntime,
    PyWebViewWindow,
    UvicornLoopbackServer,
    _configured_websocket_path,
    _create_production_server,
    start_browser_only,
    start_runtime,
)
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
class FakeClosingEvent:
    """PyWebView-shaped event that cancels native close when a handler returns False."""

    handlers: list[Any] = field(default_factory=list)

    def __iadd__(self, handler: Any) -> FakeClosingEvent:
        self.handlers.append(handler)
        return self

    def dispatch(self) -> bool:
        return any(handler() is False for handler in self.handlers)


@dataclass
class FakeWebViewEvents:
    closing: FakeClosingEvent = field(default_factory=FakeClosingEvent)


@dataclass
class FakeNativeWindow:
    """PyWebView-like fake that only destroys when its closing event permits it."""

    hidden: bool = False
    destroyed: bool = False
    events: FakeWebViewEvents = field(default_factory=FakeWebViewEvents)

    def hide(self) -> None:
        self.hidden = True

    def show(self) -> None:
        pass

    def restore(self) -> None:
        pass

    def focus(self) -> None:
        pass

    def destroy(self) -> None:
        if not self.events.closing.dispatch():
            self.destroyed = True

    def request_user_close(self) -> bool:
        """Return whether the native backend cancelled the user close request."""
        cancelled = self.events.closing.dispatch()
        if not cancelled:
            self.destroyed = True
        return cancelled


@dataclass
class FakeSession:
    state: ConnectionState = ConnectionState.IDLE
    stop_calls: int = 0
    local_capture_stop_calls: int = 0
    local_capture_stopped: bool = False

    def stop_local_capture(self) -> None:
        if not self.local_capture_stopped:
            self.local_capture_stop_calls += 1
            self.local_capture_stopped = True

    async def stop(self) -> None:
        self.stop_local_capture()
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
class FakeBrowserOnlyServerFactory:
    """Compose a loopback fake while making GUI construction impossible to hide."""

    server: FakeServer = field(default_factory=lambda: FakeServer(controller=FakeSession()))
    received_config: RuntimeConfig | None = None
    received_port: int | None = None
    window_created: bool = False

    @property
    def started(self) -> bool:
        return self.server.started

    def __call__(self, config: RuntimeConfig, port: int) -> FakeServer:
        self.received_config = config
        self.received_port = port
        self.server.url = f"http://127.0.0.1:{port}"
        return self.server


@dataclass
class FailingStopServer(FakeServer):
    """Fail exactly one controller-stop scheduling attempt without leaking a coroutine."""

    fail_stop_once: bool = True

    def run_coroutine(self, coroutine: Any) -> None:
        if self.fail_stop_once:
            self.fail_stop_once = False
            coroutine.close()
            raise RuntimeError("The controller stop failed.")
        super().run_coroutine(coroutine)


@dataclass
class LoopClosingStopServer(FakeServer):
    """Model the production server's unavailable loop after its first shutdown."""

    fail_stop_once: bool = True
    loop_closed: bool = False
    stop_schedule_attempts: int = 0

    def run_coroutine(self, coroutine: Any) -> bool:
        self.stop_schedule_attempts += 1
        if self.fail_stop_once:
            self.fail_stop_once = False
            coroutine.close()
            raise RuntimeError("The controller stop failed.")
        if self.loop_closed:
            coroutine.close()
            return False
        super().run_coroutine(coroutine)
        return True

    def shutdown(self) -> None:
        self.loop_closed = True
        super().shutdown()


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


@fixture
def fake_browser_only_server_factory() -> FakeBrowserOnlyServerFactory:
    return FakeBrowserOnlyServerFactory()


def test_browser_only_starts_the_real_loopback_server_at_the_requested_port(
    fake_browser_only_server_factory: FakeBrowserOnlyServerFactory,
) -> None:
    """A caller-selected loopback port must start without any native GUI surface."""
    config = RuntimeConfig(
        environment="local",
        server_url="http://127.0.0.1:8000",
        websocket_path="/ws/listening/",
    )

    server = start_browser_only(
        config,
        port=8765,
        server_factory=fake_browser_only_server_factory,
        wait_for_interrupt=lambda: None,
    )

    assert server.url == "http://127.0.0.1:8765"
    assert fake_browser_only_server_factory.received_config == config
    assert fake_browser_only_server_factory.received_port == 8765
    assert fake_browser_only_server_factory.started is True
    assert fake_browser_only_server_factory.window_created is False


def test_browser_only_stops_capture_before_shutting_down_the_server(
    fake_browser_only_server_factory: FakeBrowserOnlyServerFactory,
) -> None:
    """Interrupt cleanup must stop the active capture even when there is no tray runtime."""
    session = fake_browser_only_server_factory.server.controller
    assert session is not None
    session.state = ConnectionState.STREAMING

    start_browser_only(
        RuntimeConfig(
            environment="local",
            server_url="http://127.0.0.1:8000",
            websocket_path="/ws/listening/",
        ),
        port=8765,
        server_factory=fake_browser_only_server_factory,
        wait_for_interrupt=lambda: None,
    )

    assert session.local_capture_stop_calls == 1
    assert session.stop_calls == 1
    assert fake_browser_only_server_factory.server.shutdown_calls == 1


def test_browser_only_reports_an_unavailable_server_loop_after_shutting_it_down() -> None:
    """A closed loop must not let browser-only cleanup claim the capture stopped."""
    session = FakeSession(state=ConnectionState.STREAMING)
    server = LoopClosingStopServer(
        controller=session,
        fail_stop_once=False,
        loop_closed=True,
    )

    with raises(RuntimeError, match="local capture stop could not run"):
        start_browser_only(
            RuntimeConfig(
                environment="local",
                server_url="http://127.0.0.1:8000",
                websocket_path="/ws/listening/",
            ),
            port=8765,
            server_factory=lambda _config, _port: server,
            wait_for_interrupt=lambda: None,
        )

    assert session.local_capture_stop_calls == 1
    assert session.stop_calls == 0
    assert server.shutdown_calls == 1


def test_browser_only_rejects_a_port_outside_the_tcp_range() -> None:
    """An invalid requested port must fail before production services can bind it."""
    factory_called = False

    def create_server(_config: RuntimeConfig, _port: int) -> FakeServer:
        nonlocal factory_called
        factory_called = True
        return FakeServer()

    with raises(ValueError, match="between 1 and 65535"):
        start_browser_only(
            RuntimeConfig(
                environment="local",
                server_url="http://127.0.0.1:8000",
                websocket_path="/ws/listening/",
            ),
            port=0,
            server_factory=create_server,
            wait_for_interrupt=lambda: None,
        )

    assert factory_called is False


def test_browser_only_command_reuses_runtime_config_and_forwards_the_port(
    monkeypatch: Any,
) -> None:
    """The browser-only command must select the same local backend as the desktop launcher."""
    browser_only = import_module("broccoli_desktop.browser_only")
    received: list[tuple[RuntimeConfig, int]] = []

    def start(config: RuntimeConfig, *, port: int) -> None:
        received.append((config, port))

    monkeypatch.setattr(browser_only, "start_browser_only", start)

    browser_only.main(["--local", "--port", "8765"])

    assert received == [
        (
            RuntimeConfig(
                environment="local",
                server_url="http://127.0.0.1:8000",
                websocket_path=None,
            ),
            8765,
        )
    ]


def test_console_interrupt_runs_the_complete_runtime_teardown(
    fake_server: FakeServer,
    fake_window: FakeWindow,
    fake_tray: FakeTray,
    fake_dialog: FakeDialog,
) -> None:
    """Ctrl+C must close the native runtime instead of leaving Uvicorn behind."""
    previous_handler = signal.getsignal(signal.SIGINT)

    def interrupt() -> None:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)

    runtime = start_runtime(
        RuntimeConfig(
            environment="local",
            server_url="http://127.0.0.1:8000",
            websocket_path="/ws/listening/",
        ),
        server_factory=lambda _config: fake_server,
        window_factory=lambda _title, _url: fake_window,
        tray_factory=lambda _runtime: fake_tray,
        dialog=fake_dialog,
        webview_start=interrupt,
    )

    assert runtime is not None
    assert fake_server.shutdown_calls == 1
    assert fake_tray.stop_calls == 1
    assert fake_window.destroyed is True
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_window_close_hides_instead_of_stopping_capture(
    fake_window: FakeWindow, fake_tray: FakeTray, runtime: DesktopRuntime
) -> None:
    """A close request must not stop the capture that the tray keeps alive."""
    runtime.on_window_closing()

    assert fake_window.hidden is True
    assert fake_tray.running is True
    assert runtime.session is not None
    assert runtime.session.stop_calls == 0


def test_user_close_hides_but_quit_allows_the_native_window_to_destroy() -> None:
    """The close callback must cancel user close only, never runtime teardown."""
    native_window = FakeNativeWindow()
    server = FakeServer(controller=FakeSession())
    runtime = DesktopRuntime(
        server=server,
        window=PyWebViewWindow(native_window),
        tray=FakeTray(),
        dialog=FakeDialog(),
    )
    runtime.start()

    assert native_window.request_user_close() is True
    assert native_window.hidden is True
    assert native_window.destroyed is False

    runtime.request_quit()

    assert native_window.destroyed is True


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


def test_shutdown_continues_after_stop_failure_and_retries_only_that_step() -> None:
    """A failed capture stop cannot block teardown, and retry avoids duplicate cleanup."""
    session = FakeSession(state=ConnectionState.STREAMING)
    server = FailingStopServer(controller=session)
    tray = FakeTray(running=True)
    window = FakeWindow()
    runtime = DesktopRuntime(server=server, window=window, tray=tray, dialog=FakeDialog())

    with raises(RuntimeError, match="controller stop failed"):
        runtime.shutdown()

    assert session.stop_calls == 0
    assert server.shutdown_calls == 1
    assert tray.stop_calls == 1
    assert window.destroyed is True

    runtime.shutdown()

    assert session.stop_calls == 1
    assert server.shutdown_calls == 1
    assert tray.stop_calls == 1
    assert window.destroyed is True


def test_shutdown_stops_local_capture_before_the_server_loop_closes() -> None:
    """A closed loop must not turn an unrun controller stop into a successful retry."""
    session = FakeSession(state=ConnectionState.STREAMING)
    server = LoopClosingStopServer(controller=session)
    tray = FakeTray(running=True)
    window = FakeWindow()
    runtime = DesktopRuntime(server=server, window=window, tray=tray, dialog=FakeDialog())

    with raises(RuntimeError, match="controller stop failed"):
        runtime.shutdown()

    assert session.local_capture_stop_calls == 1
    assert session.stop_calls == 0
    assert server.loop_closed is True
    assert server.shutdown_calls == 1
    assert tray.stop_calls == 1
    assert window.destroyed is True

    with raises(RuntimeError, match="local capture stop could not run"):
        runtime.shutdown()

    assert session.local_capture_stop_calls == 1
    assert session.stop_calls == 0
    assert server.stop_schedule_attempts == 2
    assert server.shutdown_calls == 1
    assert tray.stop_calls == 1
    assert window.destroyed is True


def test_loopback_server_reports_when_a_coroutine_cannot_run_on_its_event_loop() -> None:
    """Runtime shutdown must distinguish an unavailable server loop from a completed stop."""
    server = UvicornLoopbackServer(
        lambda port: Services(
            credentials=FakeCredentials(),
            remote_factory=visual_test_remote_factory(),
            capture_backend=FakeCaptureBackend(),
            loopback_port=port,
        )
    )
    ran = False

    async def stop() -> None:
        nonlocal ran
        ran = True

    assert server.run_coroutine(stop()) is False
    assert ran is False


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


def test_production_server_rejects_a_missing_configured_websocket_path() -> None:
    """Production composition must fail safely instead of inventing a remote route."""
    config = RuntimeConfig(
        environment="local",
        server_url="http://127.0.0.1:8000",
        websocket_path=None,
    )

    with raises(RuntimeError, match="backend-provided WebSocket path"):
        _create_production_server(config)


def test_production_server_rejects_a_websocket_path_with_a_query_or_fragment() -> None:
    """The external route is a path component, never a full URL suffix."""
    for websocket_path in ("/backend/listening?debug=true", "/backend/listening#fragment"):
        config = RuntimeConfig(
            environment="local",
            server_url="http://127.0.0.1:8000",
            websocket_path=websocket_path,
        )

        with raises(RuntimeError, match="backend-provided WebSocket path"):
            _create_production_server(config)


def test_production_server_accepts_a_normalized_absolute_websocket_path() -> None:
    """The backend-provided path is preserved when it has no URL components."""
    assert _configured_websocket_path("/backend/listening/") == "/backend/listening/"


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
    assert bootstrap.json()["sessions"] == {"sessions": [], "next_cursor": None}
    assert bootstrap.json()["capabilities"]["history"] is True
    assert VISUAL_TEST_TOKEN not in bootstrap.text
