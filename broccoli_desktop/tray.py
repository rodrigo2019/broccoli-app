"""System tray actions for the Broccoli Desktop runtime."""

from __future__ import annotations

from typing import Any, Protocol

from broccoli_desktop.models import ConnectionState


class RuntimeProtocol(Protocol):
    """Runtime operations that tray actions may invoke."""

    @property
    def connection_state(self) -> ConnectionState: ...

    def show_window(self) -> None: ...

    def stop_capture(self) -> None: ...

    def request_quit(self) -> None: ...


class IconProtocol(Protocol):
    """Minimal icon lifecycle surface that is safe to fake in unit tests."""

    def run_detached(self) -> None: ...

    def stop(self) -> None: ...


class TrayController:
    """Own the tray actions while delegating every state transition to the runtime."""

    def __init__(self, runtime: RuntimeProtocol, *, icon: IconProtocol | None = None) -> None:
        self._runtime = runtime
        self._icon = icon

    @property
    def status_text(self) -> str:
        labels = {
            ConnectionState.IDLE: "Pronto",
            ConnectionState.STARTING: "Iniciando",
            ConnectionState.STREAMING: "Transmitindo",
            ConnectionState.RECONNECTING: "Reconectando",
            ConnectionState.STOPPED: "Parado",
            ConnectionState.FAILED: "Falha",
            ConnectionState.DEVICE_SELECTION_REQUIRED: "Dispositivo necessário",
        }
        return f"Status: {labels[self._runtime.connection_state]}"

    def start(self) -> None:
        if self._icon is not None:
            self._icon.run_detached()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()

    def show_window(self, *_arguments: Any) -> None:
        self._runtime.show_window()

    def stop_capture(self, *_arguments: Any) -> None:
        self._runtime.stop_capture()

    def request_quit(self, *_arguments: Any) -> None:
        self._runtime.request_quit()


class _PystrayIcon:
    def __init__(self, icon: Any) -> None:
        self._icon = icon

    def run_detached(self) -> None:
        self._icon.run_detached()

    def stop(self) -> None:
        self._icon.stop()


def build_system_tray(runtime: RuntimeProtocol) -> TrayController:
    """Create the real pystray menu only for the production desktop process."""
    import pystray
    from PIL import Image

    controller = TrayController(runtime)
    menu = pystray.Menu(
        pystray.MenuItem("Show Broccoli Desktop", controller.show_window),
        pystray.MenuItem(lambda _item: controller.status_text, None, enabled=False),
        pystray.MenuItem("Stop capture", controller.stop_capture),
        pystray.MenuItem("Quit", controller.request_quit),
    )
    icon = pystray.Icon(
        "broccoli-desktop",
        Image.new("RGBA", (64, 64), "#1f8b4c"),
        "Broccoli Desktop",
        menu,
    )
    controller._icon = _PystrayIcon(icon)
    return controller
