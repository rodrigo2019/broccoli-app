"""Loopback server and native-window lifecycle for Broccoli Desktop."""

from __future__ import annotations

import asyncio
import logging
import secrets
import signal
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.request import urlopen

import pyaudiowpatch
import uvicorn

from broccoli_desktop.api import LOOPBACK_HOST, Services, create_app, create_uvicorn_config
from broccoli_desktop.branding import APPLICATION_ICON, apply_taskbar_identity
from broccoli_desktop.capture import PyAudioCaptureBackend
from broccoli_desktop.config import RuntimeConfig
from broccoli_desktop.credentials import CredentialStore
from broccoli_desktop.models import ConnectionState
from broccoli_desktop.remote import HttpListeningRemote
from broccoli_desktop.settings import LocalDeviceSettings, LocalProxySettings

logger = logging.getLogger(__name__)

HEALTH_PATH = "/health"
#: The only size the window takes outside its maximized state. The user cannot
#: reach any other one -- the window is created with resizable=False.
COMPACT_WINDOW_SIZE = (1100, 600)
STARTUP_TIMEOUT_SECONDS = 10
CONSOLE_SHUTDOWN_JOIN_TIMEOUT_SECONDS = 0.25


class WindowProtocol(Protocol):
    """The small native-window surface owned by the runtime."""

    def hide(self) -> None: ...

    def show(self) -> None: ...

    def restore(self) -> None: ...

    def focus(self) -> None: ...

    def destroy(self) -> None: ...

    def bind_closing(self, handler: Callable[[], bool]) -> None: ...

    def compact(self) -> None: ...

    def expand(self) -> None: ...

    def bind_size_changed(self, handler: Callable[[bool], None]) -> None: ...


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
    window_url: str
    controller: SessionProtocol | None

    def start(self) -> bool: ...

    def run_coroutine(self, coroutine: Awaitable[None]) -> bool | None: ...

    def shutdown(self) -> None: ...

    def bind_window_mode(self, handler: Callable[[bool], None]) -> None: ...


class WindowsDialog:
    """Minimal Win32 dialogs that never render remote or credential data."""

    def confirm_quit(self) -> bool:
        from ctypes import windll

        result = windll.user32.MessageBoxW(
            None,
            "Há uma captura em andamento. Deseja pará-la e sair do Broccoli Desktop?",
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

    def compact(self) -> None:
        # restore() first: the backend resizes with SetWindowPos, which moves a
        # maximized form without clearing FormWindowState.Maximized, so the
        # window would snap back to full size on the next repaint.
        self._window.restore()
        self._window.resize(*COMPACT_WINDOW_SIZE)

    def expand(self) -> None:
        self._window.maximize()

    def bind_size_changed(self, handler: Callable[[bool], None]) -> None:
        """Report title-bar maximize and restore, whoever caused them."""
        self._window.events.maximized += lambda: handler(True)
        self._window.events.restored += lambda: handler(False)


class UvicornLoopbackServer:
    """Run the local FastAPI app on one selected loopback port in a worker thread."""

    def __init__(
        self, services_factory: Callable[[int], Services], *, port: int | None = None
    ) -> None:
        self._port = _available_loopback_port() if port is None else _validated_loopback_port(port)
        # Generated here, not left to whatever the factory happens to pass in,
        # so the production path can never produce a server with no key --
        # test_the_runtime_always_produces_a_capability_token is what checks that.
        self._capability_token = secrets.token_urlsafe(32)
        self._services = services_factory(self._port)
        self._services.capability_token = self._capability_token
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
    def capability_token(self) -> str:
        return self._capability_token

    @property
    def window_url(self) -> str:
        """The URL the native window opens. Carries the launch key once; app.js
        strips it from the address bar as soon as it has read it."""
        return f"{self.url}/?k={self._capability_token}"

    @property
    def controller(self) -> SessionProtocol | None:
        return self._services.controller

    def bind_window_mode(self, handler: Callable[[bool], None]) -> None:
        """Let the served UI move the window it is rendered inside.

        Bound after construction because the window does not exist yet when the
        server is built -- same reason capability_token is assigned here rather
        than passed in.
        """
        self._services.window_mode = handler

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
        self._services.stop_audio_level_monitor()
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
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = threading.Event()
        self._shutdown_in_progress = False
        self._shutdown_owner: threading.Thread | None = None
        self._shutdown_requested = False
        self._window_maximized = False

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
        self.window.bind_size_changed(self._remember_window_mode)
        self._tray.start()

    def on_window_closing(self) -> bool:
        """Hide the window and cancel native destruction while the tray remains alive."""
        if self._destroying_window:
            return True
        self.window.hide()
        return False

    def show_window(self) -> None:
        # Read before restoring, never after: restore() puts the window back in
        # its Normal state, and the native backend reports that as a size
        # change, so _window_maximized is False by the time restore() returns.
        maximized = self._window_maximized
        self.window.show()
        self.window.restore()
        # That same restore() is how a maximized window loses its size on the
        # way back from the tray, so re-apply what it was before.
        if maximized:
            self.window.expand()
        self.window.focus()

    def set_window_mode(self, *, maximized: bool) -> None:
        """Move the window between its only two sizes and remember which one."""
        self._window_maximized = maximized
        if maximized:
            self.window.expand()
        else:
            self.window.compact()

    def _remember_window_mode(self, maximized: bool) -> None:
        """Track sizes the user chose from the title bar, not just ours."""
        self._window_maximized = maximized

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

    @property
    def shutdown_requested(self) -> bool:
        """Return whether a console interrupt already requested shutdown."""
        with self._shutdown_lock:
            return self._shutdown_requested

    def request_shutdown(self) -> None:
        """Start console teardown without blocking Python's signal handler."""
        with self._shutdown_lock:
            if self._shutdown_requested or self._shutdown_complete.is_set():
                return
            self._shutdown_requested = True
        threading.Thread(
            target=self._run_requested_shutdown,
            name="broccoli-shutdown",
            daemon=True,
        ).start()

    def _run_requested_shutdown(self) -> None:
        try:
            self.shutdown()
        except Exception:
            # The window is already being closed and the foreground process is
            # leaving because of Ctrl+C. A remote cleanup failure must not turn
            # a successful console exit into an unhandled background exception.
            pass

    def shutdown(self) -> None:
        """Attempt every teardown step and retry only a step that previously failed."""
        current_thread = threading.current_thread()
        while True:
            with self._shutdown_lock:
                if self._shutdown_complete.is_set():
                    return
                owner = self._shutdown_owner
                if not self._shutdown_in_progress:
                    self._shutdown_in_progress = True
                    self._shutdown_owner = current_thread
                    break
            if owner is current_thread:
                return
            if owner is None:
                continue
            timeout = (
                CONSOLE_SHUTDOWN_JOIN_TIMEOUT_SECONDS
                if self.shutdown_requested
                else STARTUP_TIMEOUT_SECONDS
            )
            owner.join(timeout=timeout)
            if owner.is_alive():
                return

        failures: list[Exception] = []
        try:
            # Close the native loop first. In particular, Ctrl+C must not keep
            # the WebView's GUI loop open while a remote session is finalizing.
            self._destroying_window = True
            self._complete_teardown_step("_window_destroyed", self.window.destroy, failures)

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
            if failures:
                raise failures[0]
        except Exception:
            with self._shutdown_lock:
                self._shutdown_in_progress = False
                self._shutdown_owner = None
            raise
        else:
            with self._shutdown_lock:
                self._shutdown_in_progress = False
                self._shutdown_owner = None
                self._shutdown_complete.set()

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
        runtime_dialog.show_error("O Broccoli Desktop não conseguiu iniciar o serviço local.")
        return None
    if not server.start():
        server.shutdown()
        runtime_dialog.show_error("O Broccoli Desktop não conseguiu iniciar o serviço local.")
        return None

    runtime: DesktopRuntime | None = None
    try:
        # Before the window, never after: the taskbar button inherits the
        # process identity that exists at the moment the shell creates it.
        apply_taskbar_identity()
        create_window = window_factory or _create_pywebview_window
        window = create_window("Broccoli Desktop", server.window_url)
        create_tray = tray_factory or _create_system_tray
        tray = create_tray_placeholder(create_tray, runtime_dialog, server, window)
        runtime = DesktopRuntime(server=server, window=window, tray=tray, dialog=runtime_dialog)
        if hasattr(tray, "set_runtime"):
            tray.set_runtime(runtime)
        server.bind_window_mode(lambda maximized: runtime.set_window_mode(maximized=maximized))
        runtime.start()
    except Exception:
        if runtime is None:
            server.shutdown()
        else:
            runtime.shutdown()
        runtime_dialog.show_error("O Broccoli Desktop não conseguiu abrir a janela.")
        return None

    previous_interrupt_handler = _install_interrupt_handler(runtime)
    try:
        if webview_start is None:
            _start_pywebview(runtime)
        else:
            webview_start()
    except KeyboardInterrupt:
        # The signal handler requests asynchronous teardown and raises here so
        # the foreground process cannot remain trapped in the native event loop.
        pass
    finally:
        _restore_interrupt_handler(previous_interrupt_handler)
        try:
            runtime.shutdown()
        except Exception:
            if not runtime.shutdown_requested:
                raise
    return runtime


def _install_interrupt_handler(runtime: DesktopRuntime) -> dict[int, Any]:
    """Route console interrupts through the same teardown as native quit."""
    previous_handlers: dict[int, Any] = {}

    def handle_interrupt(_signum: int, _frame: Any) -> None:
        # Do not run blocking capture, WebSocket, tray, or server cleanup from
        # inside a Python signal handler. The shutdown worker closes the native
        # window first, while this exception releases the foreground GUI loop.
        runtime.request_shutdown()
        raise KeyboardInterrupt

    for signum in _interrupt_signals():
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, handle_interrupt)
    return previous_handlers


def _restore_interrupt_handler(previous_handlers: dict[int, Any]) -> None:
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)


def _interrupt_signals() -> tuple[int, ...]:
    signals = [signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals.append(sigbreak)
    return tuple(signals)


def start(config: RuntimeConfig) -> None:
    """Compatibility entry point used by the package console script."""
    start_runtime(config)


def start_browser_only(
    config: RuntimeConfig,
    *,
    port: int,
    server_factory: Callable[[RuntimeConfig, int], LoopbackServerProtocol] | None = None,
    wait_for_interrupt: Callable[[], None] | None = None,
    announce: Callable[[str], None] | None = None,
) -> LoopbackServerProtocol:
    """Serve the production loopback UI without constructing a native window or tray.

    ``announce`` receives the window URL once the service is healthy. Without it
    there is no way for an operator to obtain the per-launch capability token
    every /api/* request now requires: the native runtime reads it off
    ``window_url`` and the page strips it from the address bar, and this entry
    point has no window to read it from. See broccoli_desktop.browser_only,
    which prints it, and docs/desktop-live-integration.md, whose acceptance
    procedure depends on it.
    """
    loopback_port = _validated_loopback_port(port)
    server = (server_factory or _create_browser_only_server)(config, loopback_port)
    try:
        if not server.start():
            raise RuntimeError("Broccoli Desktop could not start its local service.")
        if announce is not None:
            announce(server.window_url)
        try:
            (wait_for_interrupt or _wait_for_interrupt)()
        except KeyboardInterrupt:
            pass
        return server
    finally:
        _stop_browser_only_server(server)


def _create_browser_only_server(config: RuntimeConfig, port: int) -> UvicornLoopbackServer:
    return _create_production_server(config, port=port)


def _wait_for_interrupt() -> None:
    threading.Event().wait()


def _stop_browser_only_server(server: LoopbackServerProtocol) -> None:
    """Stop capture before the loopback server, even when one cleanup step fails."""
    failures: list[Exception] = []
    session = server.controller
    if session is not None:

        def stop_controller() -> None:
            if server.run_coroutine(session.stop()) is False:
                raise RuntimeError(
                    "The local capture stop could not run because the loopback service is closed."
                )

        for action in (session.stop_local_capture, stop_controller):
            try:
                action()
            except Exception as error:
                failures.append(error)
    try:
        server.shutdown()
    except Exception as error:
        failures.append(error)
    if failures:
        raise failures[0]


def _create_production_server(
    config: RuntimeConfig, *, port: int | None = None
) -> UvicornLoopbackServer:
    def create_services(port: int) -> Services:
        # `services` is read inside the lambda, not passed to it -- the remote
        # is only built once a token exists (login, or a token change), which
        # is always after this closure returns, so by the time it runs
        # `services` is already bound. That late lookup is what lets a proxy
        # saved after startup reach the very next remote this factory builds,
        # without threading Services through HttpListeningRemote's construction.
        services = Services(
            credentials=CredentialStore(),
            remote_factory=lambda token: HttpListeningRemote(
                config.server_url,
                token,
                websocket_path=config.websocket_path,
                proxy=services.proxy_url(),
            ),
            capture_backend=PyAudioCaptureBackend(pyaudiowpatch.PyAudio()),
            loopback_port=port,
            device_settings=LocalDeviceSettings(),
            proxy_settings=LocalProxySettings(),
            backend_url=config.server_url,
        )
        return services

    return UvicornLoopbackServer(create_services, port=port)


def _available_loopback_port() -> int:
    """Reserve a free loopback port, then release it for uvicorn to rebind.

    The release and uvicorn's later bind cannot be atomic, so another process
    can claim the same port in between. Retry the reservation once on
    ``OSError`` rather than letting one bad draw fail the whole startup.
    """
    try:
        return _reserve_loopback_port()
    except OSError:
        return _reserve_loopback_port()


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind((LOOPBACK_HOST, 0))
        return int(reservation.getsockname()[1])


def _validated_loopback_port(port: int) -> int:
    """Accept one concrete TCP port for a server that always binds loopback only."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The loopback port must be an integer between 1 and 65535.")
    return port


def _create_pywebview_window(title: str, url: str) -> PyWebViewWindow:
    import webview

    width, height = COMPACT_WINDOW_SIZE
    native = webview.create_window(
        title,
        url,
        js_api=None,
        width=width,
        height=height,
        # No drag handles and no title-bar double click: the window has two
        # sizes and no others. The maximize button comes back below -- see
        # _enable_native_maximize for why that is not a contradiction.
        resizable=False,
    )
    # Only once the form exists; the instance registry is empty until then.
    native.events.shown += lambda: _enable_native_maximize(native)
    return PyWebViewWindow(native)


def _enable_native_maximize(native: Any) -> None:
    """Give the fixed-size window its title-bar maximize button back.

    ``resizable=False`` switches off MaximizeBox along with the draggable
    border, which is one control too many. The border must stay fixed, but the
    maximize button is exactly the two-state toggle this app wants: Windows
    restores to the size the window was created at, so the button moves between
    COMPACT_WINDOW_SIZE and maximized and nowhere else.

    PyWebView exposes no setting for MaximizeBox, so this reaches the form
    through its private instance registry. Guarded on purpose: an upgrade may
    move that registry, and the cost of losing this is a missing button, which
    must never be the reason the application fails to open. The events that
    keep DesktopRuntime in sync are public API and are unaffected either way.
    """
    try:
        from System import Action
        from webview.platforms.winforms import BrowserView

        form = BrowserView.instances[native.uid]
        # Assigning MaximizeBox recreates the window handle, so it has to run
        # on the UI thread that owns it.
        form.Invoke(Action(lambda: setattr(form, "MaximizeBox", True)))
    except Exception:
        logger.warning(
            "Could not re-enable the native maximize button; the window still "
            "follows sign-in and stays fixed at its two sizes.",
            exc_info=True,
        )


def _start_pywebview(runtime: DesktopRuntime) -> None:
    """Start PyWebView without letting its SIGINT handler bypass runtime teardown."""
    import webview

    # ``icon`` is documented as GTK/QT-only, but the WinForms backend reads the
    # same state and assigns it to the form -- which is the window's title bar,
    # its Alt+Tab entry, and its taskbar button. Without it that backend falls
    # back to whatever sys.executable carries: the embedded icon once
    # PyInstaller has packaged the application, and python.exe's own from a
    # source checkout.
    window_icon = str(APPLICATION_ICON)

    # PyWebView installs its own SIGINT handler while starting its Windows GUI
    # backend.  Without this hook, that replacement bypasses
    # ``_install_interrupt_handler`` above: the backend repeatedly asks the
    # native window to close, while our normal close callback keeps it alive
    # for the tray.  Marking shutdown requested first lets the existing
    # teardown worker destroy the window and its dependencies normally.
    gui = webview.initialize()
    original_interrupt_handler = getattr(gui, "_sigint_handler", None)
    if not callable(original_interrupt_handler):
        webview.start(icon=window_icon)
        return

    def handle_interrupt(signum: int, frame: Any) -> None:
        runtime.request_shutdown()
        original_interrupt_handler(signum, frame)

    gui._sigint_handler = handle_interrupt
    try:
        webview.start(icon=window_icon)
    finally:
        gui._sigint_handler = original_interrupt_handler


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
