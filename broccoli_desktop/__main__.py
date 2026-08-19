from __future__ import annotations

import sys
from collections.abc import Sequence
from importlib import import_module

from broccoli_desktop.config import parse_runtime_config


def main(argv: Sequence[str] | None = None) -> None:
    """Parse runtime configuration and start the application on invocation."""
    config = parse_runtime_config(sys.argv[1:] if argv is None else argv)
    try:
        runtime = import_module("broccoli_desktop.runtime")
    except ModuleNotFoundError as error:
        if error.name != "broccoli_desktop.runtime":
            raise
        raise RuntimeError("Desktop runtime startup is not available yet.") from None

    runtime.start(config)


if __name__ == "__main__":
    main()
