"""Runtime configuration for the desktop client."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

PRODUCTION_SERVER_URL = "https://broccoli.bosch-digital-factory.com"
DEVELOPMENT_SERVER_URL = (
    "https://broccoli-dev-app.salmonbush-5de0469c.eastus2.azurecontainerapps.io"
)
#: What `--local` points at when it is given no port of its own.
LOCAL_SERVER_PORT = 8000
#: The backend's Listening route, the same in all three environments. Compiled
#: in for the same reason the production hostname is: the client joins this
#: onto the selected server URL, so anything able to set it from outside could
#: aim a production window at a route of its choosing.
WEBSOCKET_PATH = "/ws/listening/"


@dataclass(frozen=True)
class RuntimeConfig:
    """The selected remote server configuration."""

    environment: Literal["production", "development", "local"]
    server_url: str
    websocket_path: str = WEBSOCKET_PATH


def _port_number(value: str) -> int:
    """Reject anything that cannot be a port before it is pasted into a URL."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a port number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{port} is outside the 1-65535 range")
    return port


def parse_runtime_config(argv: Sequence[str]) -> RuntimeConfig:
    """Parse command-line environment selection without loading application runtime."""
    parser = argparse.ArgumentParser(prog="broccoli-desktop")
    environment = parser.add_mutually_exclusive_group()
    # The port is the only thing that varies per machine: a platform checkout
    # served on something other than 8000. Everything else is compiled in.
    environment.add_argument("--local", nargs="?", type=_port_number, const=LOCAL_SERVER_PORT)
    environment.add_argument("--dev", action="store_true")
    arguments = parser.parse_args(argv)

    if arguments.local is not None:
        return RuntimeConfig(environment="local", server_url=f"http://127.0.0.1:{arguments.local}")
    if arguments.dev:
        return RuntimeConfig(environment="development", server_url=DEVELOPMENT_SERVER_URL)
    return RuntimeConfig(environment="production", server_url=PRODUCTION_SERVER_URL)
