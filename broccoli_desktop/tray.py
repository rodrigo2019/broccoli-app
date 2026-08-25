"""System tray actions for the Broccoli Desktop runtime."""

from __future__ import annotations

from typing import Any, Protocol

from broccoli_desktop.branding import load_icon_image
from broccoli_desktop.i18n import Translator
from broccoli_desktop.models import ConnectionState

#: One catalog key per connection state. A mapping rather than a match, so a
#: state added later fails loudly here instead of painting an empty menu line.
#: The keys are the window's own state labels: the badge in the title bar and
#: this menu line name the same thing, and two sets of wording for one concept
#: is two sets to keep in step across three languages.
_STATE_KEYS: dict[ConnectionState, str] = {
    ConnectionState.IDLE: "capture.state.idle",
    ConnectionState.STARTING: "capture.state.starting",
    ConnectionState.STREAMING: "capture.state.streaming",
    ConnectionState.RECONNECTING: "capture.state.reconnecting",
    ConnectionState.STOPPED: "capture.state.stopped",
    ConnectionState.FAILED: "capture.state.failed",
    ConnectionState.DEVICE_SELECTION_REQUIRED: "capture.state.deviceRequired",
}


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

    def __init__(
        self,
        runtime: RuntimeProtocol,
        *,
        translator: Translator,
        icon: IconProtocol | None = None,
    ) -> None:
        self._runtime = runtime
        self._translator = translator
        self._icon = icon

    @property
    def translator(self) -> Translator:
        return self._translator

    @property
    def status_text(self) -> str:
        status = self._translator.t(_STATE_KEYS[self._runtime.connection_state])
        return self._translator.t("tray.status", status=status)

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


def build_system_tray(runtime: RuntimeProtocol, translator: Translator) -> TrayController:
    """Create the real pystray menu only for the production desktop process."""
    import pystray

    # Every label is a callable, the way the status line already was: pystray
    # re-evaluates them each time Windows paints the menu, so a language chosen
    # in the settings screen reaches the notification area without a restart and
    # without rebuilding the icon.
    menu = pystray.Menu(
        pystray.MenuItem(
            lambda _item: translator.t("tray.open"), lambda _icon: controller.show_window()
        ),
        pystray.MenuItem(lambda _item: controller.status_text, None, enabled=False),
        pystray.MenuItem(
            lambda _item: translator.t("tray.stopCapture"), lambda _icon: controller.stop_capture()
        ),
        pystray.MenuItem(
            lambda _item: translator.t("tray.quit"), lambda _icon: controller.request_quit()
        ),
    )
    icon = pystray.Icon(
        "broccoli-desktop",
        load_icon_image(),
        "Broccoli Desktop",
        menu,
    )
    controller = TrayController(runtime, translator=translator, icon=_PystrayIcon(icon))
    return controller
