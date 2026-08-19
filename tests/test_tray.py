from __future__ import annotations

from dataclasses import dataclass

from broccoli_desktop.models import ConnectionState
from broccoli_desktop.tray import TrayController


@dataclass
class FakeRuntime:
    state: ConnectionState = ConnectionState.IDLE
    show_calls: int = 0
    stop_calls: int = 0
    quit_calls: int = 0

    @property
    def connection_state(self) -> ConnectionState:
        return self.state

    def show_window(self) -> None:
        self.show_calls += 1

    def stop_capture(self) -> None:
        self.stop_calls += 1

    def request_quit(self) -> None:
        self.quit_calls += 1


@dataclass
class FakeIcon:
    running: bool = False
    stop_calls: int = 0

    def run_detached(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.stop_calls += 1


def test_tray_stop_action_uses_the_runtime_capture_path() -> None:
    """The tray stop command must delegate to the same runtime path as local shutdown."""
    runtime = FakeRuntime(state=ConnectionState.STREAMING)
    tray = TrayController(runtime, icon=FakeIcon())

    tray.stop_capture()

    assert runtime.stop_calls == 1


def test_tray_show_restores_the_desktop_window() -> None:
    """The tray's Show action delegates restoration to the runtime window boundary."""
    runtime = FakeRuntime()
    tray = TrayController(runtime, icon=FakeIcon())

    tray.show_window()

    assert runtime.show_calls == 1


def test_tray_status_reflects_the_current_capture_state() -> None:
    """The non-interactive tray status must expose active capture distinctly."""
    runtime = FakeRuntime(state=ConnectionState.STREAMING)
    tray = TrayController(runtime, icon=FakeIcon())

    assert tray.status_text == "Status: Transmitindo"


def test_tray_quit_delegates_confirmation_to_the_runtime() -> None:
    """The tray never bypasses the runtime's active-capture confirmation policy."""
    runtime = FakeRuntime(state=ConnectionState.STREAMING)
    tray = TrayController(runtime, icon=FakeIcon())

    tray.request_quit()

    assert runtime.quit_calls == 1
