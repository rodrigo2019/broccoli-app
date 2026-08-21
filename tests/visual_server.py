"""Fake-only local server used by the browser visual acceptance script."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

import uvicorn

from broccoli_desktop.api import Services, create_app, create_uvicorn_config
from broccoli_desktop.models import DeviceDescriptor
from tests.fakes import (
    FakeCaptureBackend,
    visual_test_remote_factory,
)

#: Fixed so scripts/visual-check.ps1 can open the page with a key. The
#: production path generates a random one per launch; test_runtime asserts it.
VISUAL_CAPABILITY_TOKEN = "visual-capability-token"


@dataclass
class VisualCredentials:
    """Store the fake browser credential in process memory for one visual run."""

    token: str | None = None

    def load_token(self) -> str | None:
        return self.token

    def save_token(self, token: str) -> None:
        self.token = token

    def delete_token(self) -> None:
        self.token = None


def create_visual_app(*, port: int):
    """Build an app containing only deterministic task fakes and loopback metadata."""
    capture_backend = FakeCaptureBackend(
        devices=[
            DeviceDescriptor("mic-1", "Microphone One", "mic"),
            DeviceDescriptor("system-1", "Speakers", "system"),
        ]
    )
    return create_app(
        Services(
            credentials=VisualCredentials(),
            remote_factory=visual_test_remote_factory(),
            capture_backend=capture_backend,
            loopback_port=port,
            capability_token=VISUAL_CAPABILITY_TOKEN,
        )
    )


def main(argv: Sequence[str] | None = None) -> None:
    """Run the fake-only server on an explicitly selected loopback port.

    Built through ``create_uvicorn_config`` rather than ``uvicorn.run(...)``
    directly, so this fixed test token gets the same log redaction the
    production path does -- ``uvicorn.run`` builds its own ``uvicorn.Config``
    internally, with no hook to install anything on it afterward.
    """
    parser = argparse.ArgumentParser(prog="python -m tests.visual_server")
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args(argv)
    config = create_uvicorn_config(create_visual_app(port=arguments.port), port=arguments.port)
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
