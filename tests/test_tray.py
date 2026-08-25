from __future__ import annotations

from dataclasses import dataclass

from broccoli_desktop.i18n import Translator, translate
from broccoli_desktop.models import ConnectionState
from broccoli_desktop.settings import InMemoryUiSettings
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


def tray_for(runtime: FakeRuntime, *, locale: str = "pt-BR") -> TrayController:
    """A controller wired to a pinned language.

    Pinned rather than left to the default, which follows the Windows display
    language: the assertions below name what the menu says, and a suite that
    passed or failed on the language of the machine running it would not be
    checking anything.
    """
    return TrayController(
        runtime, icon=FakeIcon(), translator=Translator(InMemoryUiSettings(locale))
    )


def test_tray_stop_action_uses_the_runtime_capture_path() -> None:
    """The tray stop command must delegate to the same runtime path as local shutdown."""
    runtime = FakeRuntime(state=ConnectionState.STREAMING)
    tray = tray_for(runtime)

    tray.stop_capture()

    assert runtime.stop_calls == 1


def test_tray_show_restores_the_desktop_window() -> None:
    """The tray's Show action delegates restoration to the runtime window boundary."""
    runtime = FakeRuntime()
    tray = tray_for(runtime)

    tray.show_window()

    assert runtime.show_calls == 1


def test_tray_status_reflects_the_current_capture_state() -> None:
    """The non-interactive tray status must expose active capture distinctly."""
    runtime = FakeRuntime(state=ConnectionState.STREAMING)
    tray = tray_for(runtime)

    expected = translate(
        "pt-BR", "tray.status", status=translate("pt-BR", "capture.state.streaming")
    )
    assert tray.status_text == expected


def test_tray_status_speaks_the_chosen_language() -> None:
    """The menu is part of the application, not a fixed label beside it: a
    German window with a Portuguese notification area is the half-translated
    result this whole mechanism exists to avoid."""
    runtime = FakeRuntime(state=ConnectionState.STREAMING)

    assert tray_for(runtime, locale="de").status_text == translate(
        "de", "tray.status", status=translate("de", "capture.state.streaming")
    )


def test_tray_status_follows_a_language_chosen_after_it_was_built() -> None:
    """Windows paints this menu long after the icon is created, and the settings
    screen writes through the same store -- so the next paint has to read the
    new value rather than one captured at construction."""
    settings = InMemoryUiSettings("pt-BR")
    runtime = FakeRuntime(state=ConnectionState.STREAMING)
    tray = TrayController(runtime, icon=FakeIcon(), translator=Translator(settings))
    before = tray.status_text

    settings.save("de")

    assert tray.status_text != before
    assert tray.status_text == translate(
        "de", "tray.status", status=translate("de", "capture.state.streaming")
    )


def test_every_connection_state_has_a_tray_label_in_every_language() -> None:
    """A state added without a catalog entry would paint the menu line as its
    own key -- the one place in the application nobody is looking at while it
    happens."""
    from broccoli_desktop.i18n import SUPPORTED_UI_LOCALES
    from broccoli_desktop.tray import _STATE_KEYS

    assert set(_STATE_KEYS) == set(ConnectionState)
    for locale in SUPPORTED_UI_LOCALES:
        for key in _STATE_KEYS.values():
            assert translate(locale, key) != key, (locale, key)


def test_tray_quit_delegates_confirmation_to_the_runtime() -> None:
    """The tray never bypasses the runtime's active-capture confirmation policy."""
    runtime = FakeRuntime(state=ConnectionState.STREAMING)
    tray = tray_for(runtime)

    tray.request_quit()

    assert runtime.quit_calls == 1
