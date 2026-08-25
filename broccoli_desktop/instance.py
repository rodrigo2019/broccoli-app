"""One running copy of the application per environment, per signed-in user.

Nothing stopped a second launch: each process picks its own free loopback port,
so two of them coexist happily -- two tray icons, two windows, and two captures
competing for the same audio endpoint. The window also hides rather than closes,
which is exactly how a user ends up clicking the shortcut again.

Two named kernel objects carry that, both in the session namespace (no
``Global\\`` prefix), which scopes them to the signed-in user the way the
per-user installation already is:

* a mutex, whose existence *is* the lock. Windows destroys it when the last
  handle closes, including the ones a crashed process leaves behind, so there is
  no stale lock to detect and no PID, port, or token to keep on disk -- the
  capability token in ``broccoli_desktop.api`` stays a per-launch secret that
  never touches the filesystem.
* an auto-reset event, the "show yourself" message the losing launch sends
  before it exits.

Both are created in every launch, and the event before the mutex: a launch that
loses the race signals immediately, so the event has to already exist, and an
auto-reset event holds a signal that arrives before anyone is waiting.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from broccoli_desktop.branding import APPLICATION_IDENTITY

logger = logging.getLogger(__name__)

#: Appended to the lock's name for the activation event beside it.
ACTIVATION_SUFFIX = ".activate"
WATCHER_JOIN_TIMEOUT_SECONDS = 1.0
_ERROR_ALREADY_EXISTS = 183
_INFINITE = 0xFFFFFFFF
_WAIT_FAILED = 0xFFFFFFFF
#: AllowSetForegroundWindow's "any process may take the foreground". The
#: alternative wants the owner's process id, which this side deliberately does
#: not know: learning it would mean writing one down somewhere.
_ASFW_ANY = 0xFFFFFFFF


def instance_name(environment: str) -> str:
    """Name the lock for one environment.

    Per environment rather than per application: a checkout pointed at ``--dev``
    has to keep opening beside the installed production build, which is the
    daily development case. Two copies of the *same* environment are what this
    forbids.
    """
    return f"{APPLICATION_IDENTITY}.{environment}"


#: Repeated verbatim by the installer's ``AppMutex`` so setup and uninstall
#: refuse to write over a running copy. tests/test_packaging.py keeps the two
#: spellings equal.
PRODUCTION_INSTANCE_NAME = instance_name("production")


class Win32Protocol(Protocol):
    """The handful of Windows calls the guard makes, kept injectable so the
    handover can be tested without two real processes."""

    def create_event(self, name: str | None, *, manual_reset: bool = False) -> int: ...

    def create_mutex(self, name: str) -> tuple[int, bool]: ...

    def set_event(self, handle: int) -> bool: ...

    def wait_for_any(self, handles: Sequence[int]) -> int: ...

    def close_handle(self, handle: int) -> None: ...

    def allow_foreground_from_any_process(self) -> None: ...


class InstanceGuardProtocol(Protocol):
    """What the runtime asks of a guard, so tests can supply their own."""

    def acquire(self) -> bool: ...

    def signal_existing(self) -> bool: ...

    def watch(self, on_activate: Callable[[], None]) -> None: ...

    def release(self) -> None: ...


class _CtypesWin32:
    """The real calls, with every signature declared.

    ctypes assumes a C ``int`` return, which truncates a 64-bit HANDLE into
    something that looks valid and closes nothing, so the prototypes below are
    load-bearing rather than documentation. ``use_last_error`` is what makes
    ``ERROR_ALREADY_EXISTS`` -- the answer this module exists to read -- survive
    the return from ``CreateMutexW``.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._handle_type = wintypes.HANDLE
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32.CreateEventW.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.WaitForMultipleObjects.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        self._kernel32 = kernel32
        self._user32 = user32

    def create_event(self, name: str | None, *, manual_reset: bool = False) -> int:
        handle = self._kernel32.CreateEventW(None, manual_reset, False, name)
        return int(handle or 0)

    def create_mutex(self, name: str) -> tuple[int, bool]:
        # No initial owner: the guard never waits on this mutex and never
        # releases it. Its existence is the whole signal, and the handle held
        # open for the process lifetime is what keeps it in existence.
        handle = self._kernel32.CreateMutexW(None, False, name)
        already_existed = self._ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        return int(handle or 0), already_existed

    def set_event(self, handle: int) -> bool:
        return bool(self._kernel32.SetEvent(handle))

    def wait_for_any(self, handles: Sequence[int]) -> int:
        block = (self._handle_type * len(handles))(*handles)
        result = self._kernel32.WaitForMultipleObjects(len(handles), block, False, _INFINITE)
        if result == _WAIT_FAILED or result >= len(handles):
            return -1
        return int(result)

    def close_handle(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)

    def allow_foreground_from_any_process(self) -> None:
        self._user32.AllowSetForegroundWindow(_ASFW_ANY)


class SingleInstanceGuard:
    """Claim one environment's lock, or reach the launch that already holds it."""

    def __init__(self, environment: str, *, win32: Win32Protocol | None = None) -> None:
        self._name = instance_name(environment)
        self._win32 = win32 if win32 is not None else _CtypesWin32()
        self._activation = 0
        self._mutex = 0
        self._stop = 0
        self._watcher: threading.Thread | None = None
        self._released = False

    @property
    def name(self) -> str:
        """The lock's name, which is also the installer's ``AppMutex``."""
        return self._name

    @property
    def watcher_running(self) -> bool:
        watcher = self._watcher
        return watcher is not None and watcher.is_alive()

    def acquire(self) -> bool:
        """Return whether this launch is the one that should open the window.

        A guard is an accessory. If Windows refuses either object, this reports
        success and the application opens as it always did: a convenience must
        never be the reason nobody can work -- the same rule
        ``apply_taskbar_identity`` follows.
        """
        self._activation = self._win32.create_event(f"{self._name}{ACTIVATION_SUFFIX}")
        if self._activation == 0:
            logger.warning(
                "Could not open the activation channel for %s; a second launch will "
                "report that this one is already running instead of bringing it forward.",
                self._name,
            )
        mutex, already_existed = self._win32.create_mutex(self._name)
        if mutex == 0:
            logger.warning(
                "Could not claim the single-instance lock for %s; nothing will stop a "
                "second copy from opening.",
                self._name,
            )
            return True
        self._mutex = mutex
        return not already_existed

    def signal_existing(self) -> bool:
        """Ask the running launch to come forward, and report whether it was told.

        The foreground right is given away first, and from here rather than from
        the other side: Windows only lets the process it currently considers
        foreground raise a window, and that is this one -- the user just started
        it. Without this the running window would flash in the taskbar instead
        of opening.
        """
        if self._activation == 0:
            return False
        self._win32.allow_foreground_from_any_process()
        return self._win32.set_event(self._activation)

    def watch(self, on_activate: Callable[[], None]) -> None:
        """Run ``on_activate`` each time another launch asks for the window."""
        if self._activation == 0 or self._watcher is not None:
            return
        self._stop = self._win32.create_event(None, manual_reset=True)
        if self._stop == 0:
            logger.warning(
                "Could not create the stop signal for the activation watcher; later "
                "launches of %s will open no window at all.",
                self._name,
            )
            return
        self._watcher = threading.Thread(
            target=self._wait_for_activation,
            args=(on_activate,),
            name="broccoli-activation",
            daemon=True,
        )
        self._watcher.start()

    def release(self) -> None:
        """Stop watching and drop the lock. Safe to call more than once.

        start_runtime calls this on every way out, including the paths that
        already failed, so a second call has to cost nothing.
        """
        if self._released:
            return
        self._released = True
        if self._stop != 0:
            self._win32.set_event(self._stop)
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=WATCHER_JOIN_TIMEOUT_SECONDS)
        self._close(self._mutex)
        self._mutex = 0
        if watcher is not None and watcher.is_alive():
            # Closing a handle another thread is still waiting on is undefined.
            # The lock above is already gone, which is what the next launch
            # reads; these two die with the process moments from now.
            return
        self._close(self._activation)
        self._activation = 0
        self._close(self._stop)
        self._stop = 0

    def _wait_for_activation(self, on_activate: Callable[[], None]) -> None:
        while self._win32.wait_for_any((self._activation, self._stop)) == 0:
            try:
                on_activate()
            except Exception:
                # One window that refused to come forward must not cost every
                # later launch its handover as well.
                logger.warning("Could not bring the existing window forward.", exc_info=True)

    def _close(self, handle: int) -> None:
        if handle == 0:
            return
        try:
            self._win32.close_handle(handle)
        except Exception:
            logger.warning("Could not close a single-instance handle.", exc_info=True)
