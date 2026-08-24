from __future__ import annotations

import sys
import types
from typing import Any

from broccoli_desktop.console import ensure_standard_streams


def test_a_process_without_a_console_is_given_writable_streams(monkeypatch: Any) -> None:
    """A windowed build has no console, so Python leaves sys.stdout and
    sys.stderr as None and everything that writes to them raises on an
    attribute of NoneType. Replacing them costs nothing where they already
    exist and turns that whole class of crash into discarded output."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    ensure_standard_streams()

    assert sys.stdout is not None
    assert sys.stderr is not None
    # The two things the failure touched: writing, and being asked about a tty.
    sys.stdout.write("discarded")
    sys.stderr.write("discarded")
    assert sys.stdout.isatty() is False


def test_existing_streams_are_left_alone(monkeypatch: Any) -> None:
    """Where there is a console, its streams are the ones the operator reads --
    browser_only prints the launch URL to them, and the live-integration
    procedure depends on it."""
    original_stdout, original_stderr = sys.stdout, sys.stderr

    ensure_standard_streams()

    assert sys.stdout is original_stdout
    assert sys.stderr is original_stderr


def test_the_entry_point_repairs_the_streams_before_starting_the_runtime(
    monkeypatch: Any,
) -> None:
    """Ordering is the whole point: the runtime builds the uvicorn config on
    its way to opening the window, so a repair that happens afterwards happens
    too late. This runs main() with no streams at all and asserts they were
    already usable by the time the runtime was reached."""
    from broccoli_desktop.__main__ import main

    seen: dict[str, object] = {}

    def fake_start_runtime(_config: object) -> None:
        seen["stdout"] = sys.stdout
        seen["stderr"] = sys.stderr

    module = types.ModuleType("broccoli_desktop.runtime")
    module.start_runtime = fake_start_runtime  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "broccoli_desktop.runtime", module)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    main(["--local", "8000"])

    assert seen["stdout"] is not None
    assert seen["stderr"] is not None
