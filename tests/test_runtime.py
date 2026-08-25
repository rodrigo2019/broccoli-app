from __future__ import annotations

import asyncio
import signal
import socket
import sys
import threading
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from fastapi.testclient import TestClient
from pytest import fixture, raises

from broccoli_desktop.api import Services, create_app
from broccoli_desktop.branding import APPLICATION_ICON
from broccoli_desktop.browser_only import print_window_url
from broccoli_desktop.config import RuntimeConfig
from broccoli_desktop.i18n import translate
from broccoli_desktop.instance import SingleInstanceGuard, instance_name
from broccoli_desktop.models import ConnectionState
from broccoli_desktop.runtime import (
    COMPACT_WINDOW_SIZE,
    DesktopRuntime,
    PyWebViewWindow,
    UvicornLoopbackServer,
    _available_loopback_port,
    _create_instance_guard,
    _create_pywebview_window,
    start_browser_only,
    start_runtime,
)
from broccoli_desktop.settings import InMemoryUiSettings
from tests.fakes import (
    VISUAL_TEST_TOKEN,
    FakeCaptureBackend,
    visual_test_remote_factory,
)
from tests.visual_server import VISUAL_CAPABILITY_TOKEN, create_visual_app


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
    compacted: int = 0
    expanded: int = 0
    calls: list[str] = field(default_factory=list)
    size_changed_handler: Any = None
    #: Mimics the real window, whose restore() makes pywebview fire `restored`.
    restore_reports_restored: bool = False

    def hide(self) -> None:
        self.hidden = True

    def show(self) -> None:
        self.shown = True

    def restore(self) -> None:
        self.restored = True
        self.calls.append("restore")
        if self.restore_reports_restored and self.size_changed_handler is not None:
            self.size_changed_handler(False)

    def focus(self) -> None:
        self.focused = True
        self.calls.append("focus")

    def destroy(self) -> None:
        self.destroyed = True

    def bind_closing(self, handler: Any) -> None:
        self.close_handler = handler

    def compact(self) -> None:
        self.compacted += 1
        self.calls.append("compact")

    def expand(self) -> None:
        self.expanded += 1
        self.calls.append("expand")

    def bind_size_changed(self, handler: Any) -> None:
        self.size_changed_handler = handler

    def report_size_change(self, maximized: bool) -> None:
        """Stand in for the user clicking the title bar's own buttons."""
        self.size_changed_handler(maximized)


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
class FakeWindowEvent:
    """PyWebView-shaped event that collects handlers and calls them on demand."""

    handlers: list[Any] = field(default_factory=list)

    def __iadd__(self, handler: Any) -> FakeWindowEvent:
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in self.handlers:
            handler()


@dataclass
class FakeWebViewEvents:
    closing: FakeClosingEvent = field(default_factory=FakeClosingEvent)
    shown: FakeWindowEvent = field(default_factory=FakeWindowEvent)
    maximized: FakeWindowEvent = field(default_factory=FakeWindowEvent)
    restored: FakeWindowEvent = field(default_factory=FakeWindowEvent)


@dataclass
class FakeNativeWindow:
    """PyWebView-like fake that only destroys when its closing event permits it.

    ``focus`` is an attribute rather than a method because that is what PyWebView
    offers: a constructor flag. A fake that answered it as a method was how a
    ``TypeError`` on every route back to the window survived this suite.
    """

    hidden: bool = False
    destroyed: bool = False
    shown: int = 0
    focus: bool = True
    events: FakeWebViewEvents = field(default_factory=FakeWebViewEvents)

    def hide(self) -> None:
        self.hidden = True

    def show(self) -> None:
        self.shown += 1
        self.hidden = False

    def restore(self) -> None:
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
class FakeSizedNativeWindow:
    """PyWebView-like fake that records the order of window-size operations."""

    calls: list[Any] = field(default_factory=list)
    events: FakeWebViewEvents = field(default_factory=FakeWebViewEvents)

    def restore(self) -> None:
        self.calls.append(("restore",))

    def resize(self, width: int, height: int) -> None:
        self.calls.append(("resize", width, height))

    def maximize(self) -> None:
        self.calls.append(("maximize",))


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
    window_url: str = "http://127.0.0.1:45678/?k=fake-capability-token"
    window_mode: Any = None

    def bind_window_mode(self, handler: Any) -> None:
        self.window_mode = handler

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
class FakeGuard:
    """The single-instance lock, without a Windows kernel object behind it."""

    owner: bool = True
    can_signal: bool = True
    signals: int = 0
    releases: int = 0
    activate: Any = None

    def acquire(self) -> bool:
        return self.owner

    def signal_existing(self) -> bool:
        self.signals += 1
        return self.can_signal

    def watch(self, on_activate: Any) -> None:
        self.activate = on_activate

    def release(self) -> None:
        self.releases += 1


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


@fixture(autouse=True)
def fake_guard(monkeypatch: Any) -> FakeGuard:
    """No test may take the real single-instance lock.

    It is a named Windows object shared with anything else signed in as this
    user, so a developer with the application open would otherwise change what
    the suite does -- every start_runtime below would take the second-launch
    path and open nothing.
    """
    guard = FakeGuard()
    monkeypatch.setattr("broccoli_desktop.runtime._create_instance_guard", lambda _config: guard)
    return guard


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
    announcers: list[object] = []

    def start(
        config: RuntimeConfig, *, port: int, announce: Callable[[str], None] | None = None
    ) -> None:
        received.append((config, port))
        announcers.append(announce)

    monkeypatch.setattr(browser_only, "start_browser_only", start)

    browser_only.main(["--local", "--port", "8765"])

    assert received == [
        (
            RuntimeConfig(
                environment="local",
                server_url="http://127.0.0.1:8000",
            ),
            8765,
        )
    ]
    assert announcers == [browser_only.print_window_url]


def test_the_native_window_opens_the_url_that_carries_the_launch_key(
    fake_server: FakeServer,
    fake_tray: FakeTray,
    fake_dialog: FakeDialog,
) -> None:
    """The single line the whole capability-token mechanism hangs on.

    server.url and server.window_url differ only by the `?k=` the page reads
    once and then strips. Opening `url` instead would leave the window with no
    key at all: the shell would render and every /api/* would answer 403.
    Nothing else catches that -- the visual check drives tests/visual_server.py
    directly, not start_runtime.
    """
    opened: list[tuple[str, str]] = []

    def create_window(title: str, url: str) -> FakeWindow:
        opened.append((title, url))
        return FakeWindow()

    start_runtime(
        RuntimeConfig(
            environment="local",
            server_url="http://127.0.0.1:8000",
            websocket_path="/ws/listening/",
        ),
        server_factory=lambda _config: fake_server,
        window_factory=create_window,
        tray_factory=lambda _runtime: fake_tray,
        dialog=fake_dialog,
        webview_start=lambda: None,
    )

    assert opened == [("Broccoli Desktop", fake_server.window_url)]
    assert opened[0][1] != fake_server.url


def test_browser_only_prints_the_launch_url_the_operator_needs(
    fake_browser_only_server_factory: FakeBrowserOnlyServerFactory,
    capsys: Any,
) -> None:
    """Every /api/* request needs the per-launch capability token. The native
    runtime hands it to the window it creates; this entry point has no window,
    so without printing it nobody outside the process can obtain it -- which is
    what made the acceptance procedure in docs/desktop-live-integration.md
    impossible to run.

    The URL, not the bare token: it is what the operator pastes into the
    browser, and it is exactly what start_runtime opens.
    """
    start_browser_only(
        RuntimeConfig(
            environment="local",
            server_url="http://127.0.0.1:8000",
            websocket_path="/ws/listening/",
        ),
        port=8765,
        server_factory=fake_browser_only_server_factory,
        wait_for_interrupt=lambda: None,
        announce=print_window_url,
    )

    printed = capsys.readouterr().out.strip()
    assert printed == fake_browser_only_server_factory.server.window_url
    assert "?k=" in printed


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


def test_console_interrupt_does_not_wait_for_blocked_native_teardown(
    fake_server: FakeServer,
    fake_tray: FakeTray,
    fake_dialog: FakeDialog,
) -> None:
    """Ctrl+C must release the foreground process even if cleanup stalls."""
    entered = threading.Event()
    release = threading.Event()

    class BlockingWindow(FakeWindow):
        def destroy(self) -> None:
            entered.set()
            release.wait(timeout=5)
            super().destroy()

    window = BlockingWindow()
    started = time.monotonic()

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
        window_factory=lambda _title, _url: window,
        tray_factory=lambda _runtime: fake_tray,
        dialog=fake_dialog,
        webview_start=interrupt,
    )

    assert runtime is not None
    assert time.monotonic() - started < 1
    assert entered.wait(timeout=1)
    assert window.destroyed is False

    release.set()
    deadline = time.monotonic() + 2
    while not window.destroyed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert window.destroyed is True


def test_console_interrupt_routes_pywebviews_replaced_handler_to_runtime_teardown(
    fake_server: FakeServer,
    fake_window: FakeWindow,
    fake_tray: FakeTray,
    fake_dialog: FakeDialog,
    monkeypatch: Any,
) -> None:
    """PyWebView replacing SIGINT must still start the app's normal teardown."""
    previous_handler = signal.getsignal(signal.SIGINT)

    class FakeGui:
        interrupt_calls = 0

        def _sigint_handler(self, _signum: int, _frame: Any) -> None:
            self.interrupt_calls += 1

    gui = FakeGui()
    original_interrupt_handler = gui._sigint_handler

    class FakeWebView:
        def initialize(self) -> FakeGui:
            return gui

        def start(self, icon: str | None = None) -> None:
            # This matches PyWebView's Windows backend, which replaces the
            # application's handler after the runtime has installed it.
            signal.signal(signal.SIGINT, gui._sigint_handler)
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)

    monkeypatch.setitem(sys.modules, "webview", FakeWebView())

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
    )

    assert runtime is not None
    assert gui.interrupt_calls == 1
    assert gui._sigint_handler == original_interrupt_handler
    assert fake_server.shutdown_calls == 1
    assert fake_tray.stop_calls == 1
    assert fake_window.destroyed is True
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_the_native_window_opens_with_the_application_icon(
    fake_server: FakeServer,
    fake_window: FakeWindow,
    fake_tray: FakeTray,
    fake_dialog: FakeDialog,
    monkeypatch: Any,
) -> None:
    """Started without an icon, PyWebView's WinForms backend falls back to the
    one sys.executable carries -- python.exe's from a source checkout. The
    title bar, the Alt+Tab entry and the taskbar button all read that form
    icon, so passing ours is what puts the logo on the window at all."""

    class FakeWebView:
        def __init__(self) -> None:
            self.icon: str | None = None

        def initialize(self) -> object:
            """Stand in for a GUI backend with no handler of its own to wrap."""
            return object()

        def start(self, icon: str | None = None) -> None:
            self.icon = icon

    web_view = FakeWebView()
    monkeypatch.setitem(sys.modules, "webview", web_view)

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
    )

    assert runtime is not None
    assert web_view.icon == str(APPLICATION_ICON)


def test_the_shell_identity_is_claimed_before_the_window_exists(
    fake_server: FakeServer,
    fake_tray: FakeTray,
    fake_dialog: FakeDialog,
    monkeypatch: Any,
) -> None:
    """The shell reads a window's AppUserModelID when it creates the taskbar
    button and never revisits it, so claiming the identity after the window is
    up is the same as not claiming it: the button keeps the icon of whatever
    executable the shell derived an identity from instead of the window's own.
    """
    order: list[str] = []
    monkeypatch.setattr(
        "broccoli_desktop.runtime.apply_taskbar_identity", lambda: order.append("identity")
    )

    def create_window(_title: str, _url: str) -> FakeWindow:
        order.append("window")
        return FakeWindow()

    runtime = start_runtime(
        RuntimeConfig(
            environment="local",
            server_url="http://127.0.0.1:8000",
            websocket_path="/ws/listening/",
        ),
        server_factory=lambda _config: fake_server,
        window_factory=create_window,
        tray_factory=lambda _runtime: fake_tray,
        dialog=fake_dialog,
        webview_start=lambda: None,
    )

    assert runtime is not None
    assert order == ["identity", "window"]


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


def test_the_runtime_always_produces_a_capability_token() -> None:
    """The None escape hatch exists for the visual runner. If the production
    path could reach it, the hatch would be the hole."""
    server = UvicornLoopbackServer(
        lambda port: Services(
            credentials=FakeCredentials(),
            remote_factory=visual_test_remote_factory(),
            capture_backend=FakeCaptureBackend(),
            loopback_port=port,
        )
    )

    assert server.capability_token
    assert len(server.capability_token) >= 32
    assert server.window_url.endswith(f"/?k={server.capability_token}")


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
        ui_settings=InMemoryUiSettings("pt-BR"),
    )

    assert result is None
    assert created_windows == []
    assert server.shutdown_calls == 1
    assert dialog.errors == [translate("pt-BR", "native.error.serviceUnavailable")]


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
        ui_settings=InMemoryUiSettings("pt-BR"),
    )

    assert result is None
    assert server.shutdown_calls == 1
    assert dialog.errors == [translate("pt-BR", "native.error.windowUnavailable")]


def test_a_second_launch_opens_nothing_and_hands_the_window_over(
    fake_guard: FakeGuard, fake_dialog: FakeDialog
) -> None:
    """The point of the lock. A launch that does not hold it must not reach a
    loopback port, a window, or a second tray icon: it asks the running copy to
    come forward and leaves without a word."""
    fake_guard.owner = False
    created: list[str] = []

    result = start_runtime(
        RuntimeConfig(environment="local", server_url="http://127.0.0.1:8000"),
        server_factory=lambda _config: created.append("server") or FakeServer(),
        window_factory=lambda _title, _url: created.append("window") or FakeWindow(),
        tray_factory=lambda _runtime: FakeTray(),
        dialog=fake_dialog,
        webview_start=lambda: None,
    )

    assert result is None
    assert created == []
    assert fake_guard.signals == 1
    assert fake_guard.releases == 1
    assert fake_dialog.errors == []


def test_a_second_launch_that_cannot_reach_the_first_says_so(
    fake_guard: FakeGuard, fake_dialog: FakeDialog
) -> None:
    """Exiting in silence is right only when the running window actually comes
    forward. With no way to ask it, a user who double-clicked the shortcut would
    otherwise be left watching nothing happen."""
    fake_guard.owner = False
    fake_guard.can_signal = False

    result = start_runtime(
        RuntimeConfig(environment="local", server_url="http://127.0.0.1:8000"),
        server_factory=lambda _config: FakeServer(),
        window_factory=lambda _title, _url: FakeWindow(),
        tray_factory=lambda _runtime: FakeTray(),
        dialog=fake_dialog,
        webview_start=lambda: None,
        ui_settings=InMemoryUiSettings("pt-BR"),
    )

    assert result is None
    assert fake_dialog.errors == [translate("pt-BR", "native.error.alreadyRunning")]


def test_a_later_launch_brings_the_running_window_forward(
    fake_guard: FakeGuard, fake_window: FakeWindow, fake_server: FakeServer, fake_dialog: FakeDialog
) -> None:
    """What the second launch buys: the window returns from the notification
    area the way the tray's own menu item returns it."""
    start_runtime(
        RuntimeConfig(environment="local", server_url="http://127.0.0.1:8000"),
        server_factory=lambda _config: fake_server,
        window_factory=lambda _title, _url: fake_window,
        tray_factory=lambda _runtime: FakeTray(),
        dialog=fake_dialog,
        webview_start=lambda: None,
    )
    fake_window.shown = False
    fake_window.focused = False

    assert fake_guard.activate is not None
    fake_guard.activate()

    assert fake_window.shown is True
    assert fake_window.focused is True


def test_the_lock_is_released_on_every_way_out(fake_guard: FakeGuard) -> None:
    """A lock still held by a process on its way out is a lock nobody can take:
    the next launch would find an owner that no longer exists. Both failures end
    the launch before the window loop, and neither may skip the release."""
    releases: list[int] = []

    def failing_window(_title: str, _url: str) -> FakeWindow:
        raise RuntimeError("The native window could not be created.")

    for server, window_factory in (
        (FakeServer(healthy=False), lambda _title, _url: FakeWindow()),
        (FakeServer(), failing_window),
        (FakeServer(), lambda _title, _url: FakeWindow()),
    ):
        fake_guard.releases = 0
        start_runtime(
            RuntimeConfig(environment="local", server_url="http://127.0.0.1:8000"),
            server_factory=lambda _config, server=server: server,
            window_factory=window_factory,
            tray_factory=lambda _runtime: FakeTray(),
            dialog=FakeDialog(),
            webview_start=lambda: None,
        )
        releases.append(fake_guard.releases)

    assert releases == [1, 1, 1]


def test_the_lock_is_named_for_the_environment_being_started() -> None:
    """Production, --dev and --local take separate locks, so a checkout keeps
    opening beside the installed build."""
    guard = _create_instance_guard(
        RuntimeConfig(environment="development", server_url="http://127.0.0.1:8000")
    )

    assert isinstance(guard, SingleInstanceGuard)
    assert guard.name == instance_name("development")


def test_loopback_port_probe_retries_once_then_starts_cleanly(monkeypatch: Any) -> None:
    """A racy first reservation must not sink startup: one retry is enough."""
    attempts = 0
    real_socket = socket.socket

    class FlakyOnce:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._socket = real_socket(*args, **kwargs)

        def bind(self, address: Any) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self._socket.close()
                raise OSError("the port probe raced another process")
            self._socket.bind(address)

        def getsockname(self) -> Any:
            return self._socket.getsockname()

        def __enter__(self) -> FlakyOnce:
            return self

        def __exit__(self, *exc_info: object) -> None:
            self._socket.close()

    monkeypatch.setattr(socket, "socket", FlakyOnce)

    port = _available_loopback_port()

    assert attempts == 2
    assert 1 <= port <= 65535


def test_loopback_port_probe_gives_up_after_one_retry(monkeypatch: Any) -> None:
    """The retry is bounded to one: a second consecutive failure still surfaces."""

    class AlwaysFails:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def bind(self, address: Any) -> None:
            raise OSError("the port probe raced another process")

        def __enter__(self) -> AlwaysFails:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    monkeypatch.setattr(socket, "socket", AlwaysFails)

    with raises(OSError, match="raced"):
        _available_loopback_port()


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

    login = client.post(
        "/api/login",
        json={"token": VISUAL_TEST_TOKEN},
        headers={"X-Broccoli-Key": VISUAL_CAPABILITY_TOKEN},
    )
    with client.websocket_connect(f"/api/events?k={VISUAL_CAPABILITY_TOKEN}") as websocket:
        bootstrap = websocket.receive_json()["bootstrap"]

    assert login.status_code == 204
    assert bootstrap["authenticated"] is True
    assert [device["device_id"] for device in bootstrap["devices"]] == ["mic-1", "system-1"]
    assert VISUAL_TEST_TOKEN not in str(bootstrap)


def test_focus_reaches_the_only_activation_the_backend_offers() -> None:
    """PyWebView's Window carries `focus` as a constructor flag, so calling it
    raised TypeError and every route back to the window died there: the tray's
    "Abrir o Broccoli Desktop" item, and now a second launch handing over. The
    backend activates inside show() -- Show() then Activate() -- which is why
    that is what this asks for."""
    native = FakeNativeWindow(hidden=True)
    window = PyWebViewWindow(native)

    window.focus()

    assert native.shown == 1
    assert native.hidden is False


def test_showing_a_hidden_window_ends_with_it_in_front(fake_tray: FakeTray) -> None:
    """The whole sequence a tray click and a second launch both run: the window
    returns, takes the size it was left at, and arrives in front."""
    native = FakeNativeWindow(hidden=True)
    runtime = DesktopRuntime(
        server=FakeServer(controller=FakeSession()),
        window=PyWebViewWindow(native),
        tray=fake_tray,
        dialog=FakeDialog(),
    )

    runtime.show_window()

    assert native.hidden is False
    assert native.shown == 2


def test_compact_leaves_the_maximized_state_before_resizing() -> None:
    """SetWindowPos does not clear FormWindowState.Maximized, so restore must come first."""
    native = FakeSizedNativeWindow()

    PyWebViewWindow(native).compact()

    assert native.calls == [("restore",), ("resize", 1100, 600)]


def test_expand_maximizes_the_native_window() -> None:
    native = FakeSizedNativeWindow()

    PyWebViewWindow(native).expand()

    assert native.calls == [("maximize",)]


def test_the_native_window_is_created_fixed_at_the_compact_size(monkeypatch: Any) -> None:
    """The user gets no resize affordance; only the runtime ever changes the size."""
    received: dict[str, Any] = {}
    webview = types.ModuleType("webview")

    def create_window(title: str, url: str, **keywords: Any) -> FakeSizedNativeWindow:
        received.update(keywords)
        return FakeSizedNativeWindow()

    webview.create_window = create_window
    monkeypatch.setitem(sys.modules, "webview", webview)

    _create_pywebview_window("Broccoli Desktop", "http://127.0.0.1:8765/?k=token")

    assert (received["width"], received["height"]) == COMPACT_WINDOW_SIZE
    assert received["resizable"] is False


def test_setting_the_window_mode_expands_and_compacts_the_window() -> None:
    window = FakeWindow()
    runtime = DesktopRuntime(
        server=FakeServer(), window=window, tray=FakeTray(), dialog=FakeDialog()
    )

    runtime.set_window_mode(maximized=True)
    runtime.set_window_mode(maximized=False)

    assert window.calls == ["expand", "compact"]


def test_showing_a_maximized_window_from_the_tray_restores_its_size() -> None:
    """PyWebView's restore() drops the window to Normal, undoing the maximized state."""
    window = FakeWindow()
    runtime = DesktopRuntime(
        server=FakeServer(), window=window, tray=FakeTray(), dialog=FakeDialog()
    )
    runtime.set_window_mode(maximized=True)
    window.calls.clear()

    runtime.show_window()

    assert window.calls == ["restore", "expand", "focus"]


def test_showing_a_compact_window_from_the_tray_leaves_it_compact() -> None:
    window = FakeWindow()
    runtime = DesktopRuntime(
        server=FakeServer(), window=window, tray=FakeTray(), dialog=FakeDialog()
    )

    runtime.show_window()

    assert window.calls == ["restore", "focus"]


def test_startup_hands_the_window_mode_to_the_local_api(
    fake_server: FakeServer,
    fake_tray: FakeTray,
    fake_dialog: FakeDialog,
) -> None:
    """Only the web UI knows which screen is showing, and only it has no window."""
    window = FakeWindow()

    start_runtime(
        RuntimeConfig(
            environment="local",
            server_url="http://127.0.0.1:8000",
            websocket_path="/ws/listening/",
        ),
        server_factory=lambda _config: fake_server,
        window_factory=lambda _title, _url: window,
        tray_factory=lambda _runtime: fake_tray,
        dialog=fake_dialog,
        webview_start=lambda: None,
    )

    assert fake_server.window_mode is not None
    fake_server.window_mode(True)
    fake_server.window_mode(False)

    assert window.calls == ["expand", "compact"]


def test_the_window_reports_a_size_change_made_from_the_title_bar() -> None:
    """With MaximizeBox back on, the native button is the user's way between sizes."""
    native = FakeSizedNativeWindow()
    reported: list[bool] = []

    PyWebViewWindow(native).bind_size_changed(reported.append)
    native.events.maximized.fire()
    native.events.restored.fire()

    assert reported == [True, False]


def test_a_title_bar_maximize_survives_a_trip_to_the_tray() -> None:
    window = FakeWindow()
    runtime = DesktopRuntime(
        server=FakeServer(), window=window, tray=FakeTray(), dialog=FakeDialog()
    )
    runtime.start()
    window.report_size_change(True)
    window.calls.clear()

    runtime.show_window()

    assert window.calls == ["restore", "expand", "focus"]


def test_showing_a_window_ignores_the_restore_it_performs_itself() -> None:
    """show_window's own restore() fires `restored`; reading the mode after it loses it."""
    window = FakeWindow(restore_reports_restored=True)
    runtime = DesktopRuntime(
        server=FakeServer(), window=window, tray=FakeTray(), dialog=FakeDialog()
    )
    runtime.start()
    window.report_size_change(True)
    window.calls.clear()

    runtime.show_window()

    assert window.calls == ["restore", "expand", "focus"]
