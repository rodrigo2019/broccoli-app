"""Run Broccoli Desktop's real local service without native GUI components."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from broccoli_desktop.config import parse_runtime_config
from broccoli_desktop.runtime import start_browser_only


def main(argv: Sequence[str] | None = None) -> None:
    """Select the remote environment and serve the local UI at one explicit port."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, required=True)
    arguments, runtime_argv = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    config = parse_runtime_config(runtime_argv)
    start_browser_only(config, port=arguments.port)


if __name__ == "__main__":
    main()
