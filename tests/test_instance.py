from __future__ import annotations

import os
import threading
from typing import Any

from pytest import fixture

from broccoli_desktop.branding import APPLICATION_IDENTITY
from broccoli_desktop.instance import (
    PRODUCTION_INSTANCE_NAME,
    SingleInstanceGuard,
    instance_name,
)

#: Long enough for a signal to cross two threads on a loaded CI runner, short
#: enough that a broken handover fails the run instead of hanging it.
HANDOVER_TIMEOUT_SECONDS = 5.0


class FakeWin32:
    """One in-process stand-in for the Windows named-object namespace.

    A single instance is shared by every guard in a test, the way one machine's
    session namespace is shared by every process on it: that is what lets a test
    open two guards and watch the second one hand over to the first.
    """

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.calls: list[str] = []
        self.closed: list[int] = []
        self._named: dict[str, threading.Event] = {}
        self._mutexes: set[str] = set()
        self._objects: dict[int, threading.Event] = {}
        self._manual: dict[int, bool] = {}
        self._next_handle = 1

    def create_event(self, name: str | None, *, manual_reset: bool = False) -> int:
        self.calls.append(f"create_event:{name}")
        if "create_event" in self.failing:
            return 0
        if name is None:
            underlying = threading.Event()
        else:
            underlying = self._named.setdefault(name, threading.Event())
        return self._register(underlying, manual_reset=manual_reset)

    def create_mutex(self, name: str) -> tuple[int, bool]:
        self.calls.append(f"create_mutex:{name}")
        if "create_mutex" in self.failing:
            return 0, False
        existed = name in self._mutexes
        self._mutexes.add(name)
        return self._register(threading.Event(), manual_reset=True), existed

    def set_event(self, handle: int) -> bool:
        self.calls.append("set_event")
        if "set_event" in self.failing:
            return False
        self._objects[handle].set()
        return True

    def wait_for_any(self, handles: tuple[int, ...]) -> int:
        while True:
            for index, handle in enumerate(handles):
                underlying = self._objects[handle]
                if underlying.is_set():
                    if not self._manual[handle]:
                        underlying.clear()
                    return index
            threading.Event().wait(0.005)

    def close_handle(self, handle: int) -> None:
        self.calls.append("close_handle")
        self.closed.append(handle)

    def allow_foreground_from_any_process(self) -> None:
        self.calls.append("allow_foreground")

    def _register(self, underlying: threading.Event, *, manual_reset: bool) -> int:
        handle = self._next_handle
        self._next_handle += 1
        self._objects[handle] = underlying
        self._manual[handle] = manual_reset
        return handle


@fixture
def win32() -> FakeWin32:
    return FakeWin32()


def test_only_the_first_launch_of_an_environment_takes_the_lock(win32: FakeWin32) -> None:
    """The whole point: a second copy of the same environment must not go on to
    open a loopback port, a window, and a second tray icon."""
    first = SingleInstanceGuard("production", win32=win32)
    second = SingleInstanceGuard("production", win32=win32)

    assert first.acquire() is True
    assert second.acquire() is False


def test_a_second_launch_brings_the_running_window_forward(win32: FakeWin32) -> None:
    """The handover that replaces the second window: the running instance runs
    the same show_window path the tray's "Abrir" item uses."""
    running = SingleInstanceGuard("production", win32=win32)
    running.acquire()
    activated = threading.Event()
    running.watch(activated.set)

    launcher = SingleInstanceGuard("production", win32=win32)
    launcher.acquire()

    assert launcher.signal_existing() is True
    assert activated.wait(HANDOVER_TIMEOUT_SECONDS) is True
    running.release()
    launcher.release()


def test_the_foreground_right_is_handed_over_before_the_signal(win32: FakeWin32) -> None:
    """Windows only lets the process it considers foreground raise a window, and
    that is the one the user just launched -- not the one already running. Giving
    the right away after signalling would arrive too late, and the existing
    window would flash in the taskbar instead of coming forward."""
    running = SingleInstanceGuard("production", win32=win32)
    running.acquire()
    launcher = SingleInstanceGuard("production", win32=win32)
    launcher.acquire()
    win32.calls.clear()

    launcher.signal_existing()

    assert win32.calls == ["allow_foreground", "set_event"]


def test_the_activation_channel_exists_before_the_lock_is_claimed(win32: FakeWin32) -> None:
    """Two launches racing each other: the loser signals as soon as it loses, so
    the event has to exist by then. Creating it before the mutex -- in every
    launch, winner or loser -- is what removes that race."""
    guard = SingleInstanceGuard("production", win32=win32)

    guard.acquire()

    assert win32.calls[:2] == [
        f"create_event:{PRODUCTION_INSTANCE_NAME}.activate",
        f"create_mutex:{PRODUCTION_INSTANCE_NAME}",
    ]


def test_a_lock_that_cannot_be_created_still_lets_the_application_open() -> None:
    """A guard is an accessory: refusing to start because Windows would not hand
    out a mutex would turn a convenience into the reason nobody can work."""
    guard = SingleInstanceGuard("production", win32=FakeWin32(failing=frozenset({"create_mutex"})))

    assert guard.acquire() is True


def test_a_launch_that_cannot_signal_reports_it() -> None:
    """The runtime shows the "already running" dialog on a False here, so a
    silent exit never leaves a user staring at nothing after a double click."""
    guard = SingleInstanceGuard("production", win32=FakeWin32(failing=frozenset({"create_event"})))
    guard.acquire()

    assert guard.signal_existing() is False


def test_a_failing_activation_handler_keeps_the_watcher_alive(win32: FakeWin32) -> None:
    """One failed attempt to show the window must not cost every later one."""
    running = SingleInstanceGuard("production", win32=win32)
    running.acquire()
    attempts: list[int] = []
    first_attempt = threading.Event()
    second_attempt = threading.Event()

    def show() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            first_attempt.set()
            raise RuntimeError("the window refused to come forward")
        second_attempt.set()

    running.watch(show)
    launcher = SingleInstanceGuard("production", win32=win32)
    launcher.acquire()

    launcher.signal_existing()
    # Only once the first signal has been consumed: an auto-reset event that is
    # set twice before anyone waits on it delivers one wake-up, not two -- true
    # of the real object as much as of the fake.
    assert first_attempt.wait(HANDOVER_TIMEOUT_SECONDS) is True
    launcher.signal_existing()

    assert second_attempt.wait(HANDOVER_TIMEOUT_SECONDS) is True
    running.release()


def test_release_stops_the_watcher_and_is_idempotent(win32: FakeWin32) -> None:
    """Called on every exit path of start_runtime, including the ones that
    already failed, so a second call must be free."""
    guard = SingleInstanceGuard("production", win32=win32)
    guard.acquire()
    guard.watch(lambda: None)

    guard.release()
    closed_after_first = list(win32.closed)
    guard.release()

    assert guard.watcher_running is False
    assert len(closed_after_first) == 3
    assert win32.closed == closed_after_first


def test_each_environment_takes_its_own_lock() -> None:
    """A checkout pointed at dev has to keep opening next to the installed
    production build; two copies of the *same* environment are what this
    forbids."""
    assert instance_name("production") == PRODUCTION_INSTANCE_NAME
    assert PRODUCTION_INSTANCE_NAME.startswith(f"{APPLICATION_IDENTITY}.")
    assert instance_name("development") != PRODUCTION_INSTANCE_NAME
    assert instance_name("local") != PRODUCTION_INSTANCE_NAME


def test_two_real_windows_guards_hand_over_to_the_first() -> None:
    """The same exchange as above, against the real kernel objects rather than
    the fake namespace: nothing else in the suite would catch a truncated 64-bit
    handle, a wrong CreateMutexW argument, or a lost ERROR_ALREADY_EXISTS."""
    environment = f"pytest-{os.getpid()}"
    running = SingleInstanceGuard(environment)
    launcher = SingleInstanceGuard(environment)
    activated = threading.Event()
    try:
        assert running.acquire() is True
        running.watch(activated.set)

        assert launcher.acquire() is False
        assert launcher.signal_existing() is True
        assert activated.wait(HANDOVER_TIMEOUT_SECONDS) is True
    finally:
        launcher.release()
        running.release()


def test_a_released_real_lock_can_be_taken_again() -> None:
    """The mutex has to disappear with the process that held it -- the property
    a lock file would not have after a crash."""
    environment = f"pytest-reuse-{os.getpid()}"
    first = SingleInstanceGuard(environment)
    assert first.acquire() is True
    first.release()

    second = SingleInstanceGuard(environment)
    try:
        assert second.acquire() is True
    finally:
        second.release()


def test_the_real_guard_reports_a_watcher_only_while_it_runs() -> None:
    """Guards the join in release(): a watcher left behind would keep the
    activation handles open after the runtime has finished with them."""
    guard = SingleInstanceGuard(f"pytest-watch-{os.getpid()}")
    guard.acquire()
    guard.watch(lambda: None)

    assert guard.watcher_running is True

    guard.release()

    assert guard.watcher_running is False


def test_the_guard_answers_every_name_the_runtime_protocol_promises() -> None:
    """start_runtime holds the guard behind a Protocol; keep the concrete class
    answering each name that protocol names."""
    guard: Any = SingleInstanceGuard("production", win32=FakeWin32())

    for name in ("acquire", "signal_existing", "watch", "release"):
        assert callable(getattr(guard, name))
