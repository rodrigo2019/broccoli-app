"""Loopback server and native-window lifecycle for Broccoli Desktop."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import urlopen

import pyaudiowpatch
import uvicorn

from broccoli_desktop.api import LOOPBACK_HOST, Services, create_app, create_uvicorn_config
from broccoli_desktop.capture import PyAudioCaptureBackend
from broccoli_desktop.config import (
    BACKEND_WEBSOCKET_PATH_ENVIRONMENT_VARIABLE,
    RuntimeConfig,
)
from broccoli_desktop.credentials import CredentialStore
from broccoli_desktop.models import ConnectionState
from broccoli_desktop.remote import HttpListeningRemote

HEALTH_PATH = "/health"
STARTUP_TIMEOUT_SECONDS = 10


class WindowProtocol(Protocol):
    """The small native-window surface owned by the runtime."""

    def hide(self) -> None: ...

    def show(self) -> None: ...

    def restore(self) -> None: ...

    def focus(self) -> None: ...

    def destroy(self) -> None: ...

    def bind_closing(self, handler: Callable[[], bool]) -> None: ...


class TrayProtocol(Protocol):
    """Tray lifecycle methods used without importing a GUI engine in tests."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


class DialogProtocol(Protocol):
    """Native dialogs used for startup failures and active-capture confirmation."""

    def confirm_quit(self) -> bool: ...

    def show_error(self, message: str) -> None: ...


class SessionProtocol(Protocol):
    """Controller surface shared by the local API and tray lifecycle."""

    state: ConnectionState

    def stop_local_capture(self) -> None: ...

    async def stop(self) -> None: ...


class LoopbackServerProtocol(Protocol):
    """Managed loopback server with a controller available after local login."""

    url: str
    controller: SessionProtocol | None

    def start(self) -> bool: ...

    def run_coroutine(self, coroutine: Awaitable[None]) -> bool | None: ...

    def shutdown(self) -> None: ...


class WindowsDialog:
    """Minimal Win32 dialogs that never render remote or credential data."""

    def confirm_quit(self) -> bool:
        from ctypes import windll

        result = windll.user32.MessageBoxW(
            None,
            "A capture is active. Stop it and quit Broccoli Desktop?",
            "Broccoli Desktop",
            0x00000024,
        )
        return result == 6

    def show_error(self, message: str) -> None:
        from ctypes import windll

        windll.user32.MessageBoxW(None, message, "Broccoli Desktop", 0x00000010)


class PyWebViewWindow:
    """Adapt one PyWebView window without publishing any JavaScript API."""

    def __init__(self, window: Any) -> None:
        self._window = window

    def hide(self) -> None:
        self._window.hide()

    def show(self) -> None:
        self._window.show()

    def restore(self) -> None:
        self._window.restore()

    def focus(self) -> None:
        self._window.focus()

    def destroy(self) -> None:
        self._window.destroy()

    def bind_closing(self, handler: Callable[[], bool]) -> None:
        self._window.events.closing += lambda *_arguments: handler()


class UvicornLoopbackServer:
    """Run the local FastAPI app on one selected loopback port in a worker thread."""

    def __init__(self, services_factory: Callable[[int], Services]) -> None:
        self._port = _available_loopback_port()
        self._services = services_factory(self._port)
        self._server = uvicorn.Server(
            create_uvicorn_config(create_app(self._services), port=self._port)
        )
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown = False

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self._port}"

    @property
    def controller(self) -> SessionProtocol | None:
        return self._services.controller

    def start(self) -> bool:
        if self._thread is not None:
            return self._wait_until_healthy()
        self._thread = threading.Thread(target=self._run, name="broccoli-loopback", daemon=True)
        self._thread.start()
        return self._wait_until_healthy()

    def run_coroutine(self, coroutine: Awaitable[None]) -> bool:
        loop = self._loop
        if loop is None or loop.is_closed():
            coroutine.close()
            return False
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        future.result(timeout=STARTUP_TIMEOUT_SECONDS)
        return True

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._server.should_exit = True
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=STARTUP_TIMEOUT_SECONDS)

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._server.serve()

    def _wait_until_healthy(self) -> bool:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._server.should_exit:
                return False
            thread = self._thread
            if thread is not None and not thread.is_alive():
                return False
            try:
                with urlopen(f"{self.url}{HEALTH_PATH}", timeout=0.25) as response:
                    if response.status == 200:
                        return True
            except OSError:
                time.sleep(0.05)
        return False


class DesktopRuntime:
    """Coordinate the window, tray, server, and capture shutdown lifetime."""

    def __init__(
        self,
        *,
        server: LoopbackServerProtocol,
        window: WindowProtocol,
        tray: TrayProtocol,
        dialog: DialogProtocol,
    ) -> None:
        self._server = server
        self.window = window
        self._tray = tray
        self._dialog = dialog
        self._local_capture_stopped = False
        self._capture_stopped = False
        self._server_stopped = False
        self._tray_stopped = False
        self._window_destroyed = False
        self._destroying_window = False

    @property
    def session(self) -> SessionProtocol | None:
        return self._server.controller

    @property
    def connection_state(self) -> ConnectionState:
        session = self.session
        if session is None:
            return ConnectionState.IDLE
        return session.state

    def start(self) -> None:
        self.window.bind_closing(self.on_window_closing)
        self._tray.start()

    def on_window_closing(self) -> bool:
        """Hide the window and cancel native destruction while the tray remains alive."""
        if self._destroying_window:
            return True
        self.window.hide()
        return False

    def show_window(self) -> None:
        self.window.show()
        self.window.restore()
        self.window.focus()

    def stop_capture(self) -> None:
        """Schedule the same controller stop coroutine used by the local API."""
        session = self.session
        if session is not None:
            session.stop_local_capture()
            self._stop_controller(session)

    def request_quit(self) -> None:
        session = self.session
        if session is not None and session.state in _ACTIVE_CAPTURE_STATES:
            if not self._dialog.confirm_quit():
                return
        self.shutdown()

    def shutdown(self) -> None:
        """Attempt every teardown step and retry only a step that previously failed."""
        failures: list[Exception] = []
        session = self.session
        if not self._capture_stopped:
            if session is None or session.state not in _ACTIVE_CAPTURE_STATES:
                self._local_capture_stopped = True
                self._capture_stopped = True
            else:
                self._complete_teardown_step(
                    "_local_capture_stopped", session.stop_local_capture, failures
                )
                self._complete_teardown_step(
                    "_capture_stopped", lambda: self._stop_controller(session), failures
                )
        self._complete_teardown_step("_server_stopped", self._server.shutdown, failures)
        self._complete_teardown_step("_tray_stopped", self._tray.stop, failures)
        self._destroying_window = True
        self._complete_teardown_step("_window_destroyed", self.window.destroy, failures)
        if failures:
            raise failures[0]

    def _stop_controller(self, session: SessionProtocol) -> None:
        """Run the normal controller shutdown path or retain it for a later retry."""
        if self._server.run_coroutine(session.stop()) is False:
            raise RuntimeError(
                "The local capture stop could not run because the loopback service is closed."
            )

    def _complete_teardown_step(
        self, attribute: str, action: Callable[[], None], failures: list[Exception]
    ) -> None:
        """Run one cleanup action once, retaining it for a later retry if it fails."""
        if getattr(self, attribute):
            return
        try:
            action()
        except Exception as error:
            failures.append(error)
        else:
            setattr(self, attribute, True)


_ACTIVE_CAPTURE_STATES = frozenset(
    {
        ConnectionState.STARTING,
        ConnectionState.STREAMING,
        ConnectionState.RECONNECTING,
    }
)


def start_runtime(
    config: RuntimeConfig,
    *,
    server_factory: Callable[[RuntimeConfig], LoopbackServerProtocol] | None = None,
    window_factory: Callable[[str, str], WindowProtocol] | None = None,
    tray_factory: Callable[[DesktopRuntime], TrayProtocol] | None = None,
    dialog: DialogProtocol | None = None,
    webview_start: Callable[[], None] | None = None,
) -> DesktopRuntime | None:
    """Start the loopback service before creating the no-binding native window."""
    runtime_dialog = dialog or WindowsDialog()
    try:
        server = (server_factory or _create_production_server)(config)
    except RuntimeError:
        runtime_dialog.show_error("Broccoli Desktop could not start its local service.")
        return None
    if not server.start():
        server.shutdown()
        runtime_dialog.show_error("Broccoli Desktop could not start its local service.")
        return None

    runtime: DesktopRuntime | None = None
    try:
        create_window = window_factory or _create_pywebview_window
        window = create_window("Broccoli Desktop", server.url)
        create_tray = tray_factory or _create_system_tray
        tray = create_tray_placeholder(create_tray, runtime_dialog, server, window)
        runtime = DesktopRuntime(server=server, window=window, tray=tray, dialog=runtime_dialog)
        if hasattr(tray, "set_runtime"):
            tray.set_runtime(runtime)
        runtime.start()
    except Exception:
        if runtime is None:
            server.shutdown()
        else:
            runtime.shutdown()
        runtime_dialog.show_error("Broccoli Desktop could not open its window.")
        return None

    try:
        (webview_start or _start_pywebview)()
    finally:
        runtime.shutdown()
    return runtime


def start(config: RuntimeConfig) -> None:
    """Compatibility entry point used by the package console script."""
    start_runtime(config)


def _create_production_server(config: RuntimeConfig) -> UvicornLoopbackServer:
    websocket_path = _configured_websocket_path(config.websocket_path)

    def create_services(port: int) -> Services:
        return Services(
            credentials=CredentialStore(),
            remote_factory=lambda token: HttpListeningRemote(
                config.server_url, token, websocket_path=websocket_path
            ),
            capture_backend=PyAudioCaptureBackend(pyaudiowpatch.PyAudio()),
            loopback_port=port,
            official_broccoli_url=config.server_url,
        )

    return UvicornLoopbackServer(create_services)


def _configured_websocket_path(path: str | None) -> str:
    """Accept the caller-supplied external path only, never a guessed route."""
    if path is None:
        raise RuntimeError(
            f"{BACKEND_WEBSOCKET_PATH_ENVIRONMENT_VARIABLE} must contain the backend-provided "
            "WebSocket path as a normalized absolute URI path."
        )
    parts = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parts.scheme
        or parts.netloc
        or parts.query
        or parts.fragment
        or parts.path != path
    ):
        raise RuntimeError(
            f"{BACKEND_WEBSOCKET_PATH_ENVIRONMENT_VARIABLE} must contain the backend-provided "
            "WebSocket path as a normalized absolute URI path."
        )
    return path


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind((LOOPBACK_HOST, 0))
        return int(reservation.getsockname()[1])


def _create_pywebview_window(title: str, url: str) -> PyWebViewWindow:
    import webview

    return PyWebViewWindow(webview.create_window(title, url, js_api=None))


def _start_pywebview() -> None:
    import webview

    webview.start()


def _create_system_tray(runtime: DesktopRuntime) -> TrayProtocol:
    from broccoli_desktop.tray import build_system_tray

    return build_system_tray(runtime)


def create_tray_placeholder(
    tray_factory: Callable[[DesktopRuntime], TrayProtocol],
    dialog: DialogProtocol,
    server: LoopbackServerProtocol,
    window: WindowProtocol,
) -> TrayProtocol:
    """Build a tray after the runtime reference exists without exposing GUI imports to tests."""
    return _DeferredTray(tray_factory, dialog, server, window)


class _DeferredTray:
    """Create the concrete tray lazily once ``DesktopRuntime`` has been composed."""

    def __init__(
        self,
        tray_factory: Callable[[DesktopRuntime], TrayProtocol],
        dialog: DialogProtocol,
        server: LoopbackServerProtocol,
        window: WindowProtocol,
    ) -> None:
        self._tray_factory = tray_factory
        self._dialog = dialog
        self._server = server
        self._window = window
        self._runtime: DesktopRuntime | None = None
        self._tray: TrayProtocol | None = None

    def set_runtime(self, runtime: DesktopRuntime) -> None:
        self._runtime = runtime
        self._tray = self._tray_factory(runtime)

    def start(self) -> None:
        if self._tray is not None:
            self._tray.start()

    def stop(self) -> None:
        if self._tray is not None:
            self._tray.stop()
