"""Standard streams for a build that has no console to write to.

PyInstaller builds this application windowed (``console=False``), so a launch
from Explorer, the Start Menu or a shortcut gives the process no console at
all. Python answers that by leaving ``sys.stdout`` and ``sys.stderr`` as
``None``, and every library that writes a line or asks whether it is talking to
a terminal then raises on an attribute of ``NoneType``.

Uvicorn is the one that bit: constructing its ``Config`` runs a ``dictConfig``
whose default formatter calls ``sys.stdout.isatty()`` and whose handlers stream
to ``sys.stdout`` and ``sys.stderr``, so the packaged application died on
startup, on every launch, before its window could open.

Running the same executable from a shell hides all of it -- the process
inherits the shell's streams -- which is why this survived every build.
"""

from __future__ import annotations

import io
import os
import sys
from typing import TextIO


def ensure_standard_streams() -> None:
    """Give the process writable standard streams if it was started without any.

    Output goes to the null device rather than a file: with no console there is
    nobody to read it, and inventing a log file is a promise about disk, size
    and privacy that this call has no business making on its own.

    A no-op wherever the streams already exist, so the console entry points
    keep the ones an operator is reading -- ``browser_only`` prints the launch
    URL to them, and the live-integration procedure depends on that.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _null_stream())


class _DiscardedOutput(io.TextIOWrapper):
    """A stream that goes nowhere and does not claim to be a terminal.

    Windows reports NUL as a character device, so a plain handle on it
    answers isatty() with True -- and that answer is exactly how uvicorn
    decides to write ANSI colour codes. Nothing reads this stream, so
    nothing should be coloured for it either.
    """

    def isatty(self) -> bool:
        return False


def _null_stream() -> TextIO:
    # The handle stays open for the life of the process by design: closing
    # it would put back the None this exists to remove.
    return _DiscardedOutput(open(os.devnull, "wb"), encoding="utf-8", errors="replace")
