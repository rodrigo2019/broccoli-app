"""Run Broccoli Desktop's real local service without native GUI components."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from broccoli_desktop.config import parse_runtime_config
from broccoli_desktop.runtime import start_browser_only


def print_window_url(window_url: str) -> None:
    """Hand the operator the URL to open, launch key included.

    Every /api/* request needs the capability token generated fresh at each
    launch. The native runtime passes it to the window it creates; this entry
    point has no window, so without printing it here nobody outside the process
    can obtain it -- which left the acceptance procedure in
    docs/desktop-live-integration.md impossible to run.

    Printing it is right for *this* module specifically: it exists only to serve
    the local UI to a browser for QA, the token is worthless after the process
    exits, and the service it opens is loopback-only.
    """
    print(window_url, flush=True)


def main(argv: Sequence[str] | None = None) -> None:
    """Select the remote environment and serve the local UI at one explicit port."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, required=True)
    arguments, runtime_argv = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    config = parse_runtime_config(runtime_argv)
    start_browser_only(config, port=arguments.port, announce=print_window_url)


if __name__ == "__main__":
    main()
