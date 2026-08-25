"""Fake-only local server used by the browser visual acceptance script."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

import uvicorn

from broccoli_desktop.api import Services, create_app, create_uvicorn_config
from broccoli_desktop.autoproxy import ResolvedProxy
from broccoli_desktop.i18n import SUPPORTED_UI_LOCALES
from broccoli_desktop.models import DeviceDescriptor
from broccoli_desktop.settings import InMemoryUiSettings
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
    proxy_password: str | None = None

    def load_token(self) -> str | None:
        return self.token

    def save_token(self, token: str) -> None:
        self.token = token

    def delete_token(self) -> None:
        self.token = None

    def load_proxy_password(self) -> str | None:
        return self.proxy_password

    def save_proxy_password(self, password: str) -> None:
        self.proxy_password = password

    def delete_proxy_password(self) -> None:
        self.proxy_password = None


async def _visual_proxy_prober(_target_url: str, _proxy_url: str | None) -> bool:
    """Always succeed: this is a fake-only offline server, so "Testar conexão"
    must never open a real socket the way the production prober does."""
    return True


def _visual_script_resolver(_target_url: str, _script_url: str) -> ResolvedProxy:
    """Answer without WinHTTP, which would download a real script over the
    real network -- the same reason the prober above is faked."""
    return ResolvedProxy(host="proxy.visual.local", port=8080)


def create_visual_app(*, port: int, locale: str = "pt-BR"):
    """Build an app containing only deterministic task fakes and loopback metadata.

    The interface language is pinned, like the credential store, the proxy
    prober and the script resolver above: scripts/visual-check.ps1 waits on
    literal page text, and the production default follows the Windows display
    language -- which would make the gate pass or hang depending on whose
    machine it ran on.
    """
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
            proxy_prober=_visual_proxy_prober,
            script_resolver=_visual_script_resolver,
            ui_settings=InMemoryUiSettings(locale),
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
    parser.add_argument("--locale", default="pt-BR", choices=SUPPORTED_UI_LOCALES)
    arguments = parser.parse_args(argv)
    config = create_uvicorn_config(
        create_visual_app(port=arguments.port, locale=arguments.locale), port=arguments.port
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
