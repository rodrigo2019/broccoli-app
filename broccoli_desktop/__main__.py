from __future__ import annotations

import sys
from collections.abc import Sequence
from importlib import import_module

from broccoli_desktop import splash
from broccoli_desktop.config import parse_runtime_config
from broccoli_desktop.console import ensure_standard_streams


def main(argv: Sequence[str] | None = None) -> None:
    """Parse runtime configuration and start the application on invocation."""
    # First, before anything that might write a line or ask whether it is
    # talking to a terminal. The packaged build is windowed, so a launch from
    # Explorer leaves sys.stdout and sys.stderr as None, and uvicorn's logging
    # setup -- reached below, through the runtime -- calls isatty() on one of
    # them while the window is still opening.
    ensure_standard_streams()
    # The first thing on screen, and the reason it is here rather than inside
    # the runtime: the import below is the single longest step in a cold start
    # -- pythonnet, PortAudio, numpy, PIL and uvicorn, all of it before any
    # window exists -- so the splash has to be told what is happening while it
    # is still the only thing the user can see. broccoli_desktop.splash reaches
    # the stored language through i18n and settings alone, neither of which is
    # on that heavy path.
    splash.step(splash.LOADING)
    config = parse_runtime_config(sys.argv[1:] if argv is None else argv)
    try:
        runtime = import_module("broccoli_desktop.runtime")
    except ModuleNotFoundError as error:
        if error.name != "broccoli_desktop.runtime":
            raise
        raise RuntimeError("Desktop runtime startup is not available yet.") from None

    start_runtime = getattr(runtime, "start_runtime", None)
    if start_runtime is None:
        start_runtime = runtime.start
    start_runtime(config)


if __name__ == "__main__":
    main()
