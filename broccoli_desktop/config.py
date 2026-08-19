"""Runtime configuration for the desktop client."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

PRODUCTION_SERVER_URL = "https://broccoli.bosch-digital-factory.com"
LOCAL_SERVER_URL = "http://127.0.0.1:8000"
DEV_URL_ENVIRONMENT_VARIABLE = "BROCCOLI_DESKTOP_DEV_URL"


@dataclass(frozen=True)
class RuntimeConfig:
    """The selected remote server configuration."""

    environment: Literal["production", "development", "local"]
    server_url: str


def parse_runtime_config(argv: Sequence[str]) -> RuntimeConfig:
    """Parse command-line environment selection without loading application runtime."""
    parser = argparse.ArgumentParser(prog="broccoli-desktop")
    environment = parser.add_mutually_exclusive_group()
    environment.add_argument("--local", action="store_true")
    environment.add_argument("--dev", action="store_true")
    arguments = parser.parse_args(argv)

    if arguments.local:
        return RuntimeConfig(environment="local", server_url=LOCAL_SERVER_URL)
    if arguments.dev:
        server_url = os.environ.get(DEV_URL_ENVIRONMENT_VARIABLE)
        if not server_url:
            parser.error(f"--dev requires {DEV_URL_ENVIRONMENT_VARIABLE} to be set")
        return RuntimeConfig(environment="development", server_url=server_url)
    return RuntimeConfig(environment="production", server_url=PRODUCTION_SERVER_URL)
