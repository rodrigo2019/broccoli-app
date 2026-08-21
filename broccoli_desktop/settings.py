"""Local storage for the two opaque selected-device identities only."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from broccoli_desktop.session import CaptureChoices

_SETTINGS_DIRECTORY = "Broccoli Desktop"
_SETTINGS_FILENAME = "device-selections.json"
_SELECTION_KEYS = frozenset({"microphone_id", "system_device_id"})


class DeviceSettings(Protocol):
    """The minimal persisted-selection boundary used by local application services."""

    def load(self) -> CaptureChoices | None: ...

    def save(self, selection: CaptureChoices) -> None: ...

    def clear(self) -> None: ...


@dataclass(frozen=True)
class ProxySettings:
    """Proxy connection settings the local API persists -- deliberately with no
    password field. The password lives only in CredentialStore; keeping it out
    of this shape is what stops it from ever landing in the JSON file this is
    saved to."""

    host: str = ""
    port: int = 0
    username: str = ""
    enabled: bool = False


DEFAULT_PROXY_SETTINGS = ProxySettings()

_PROXY_SETTINGS_FILENAME = "proxy-settings.json"
_PROXY_SETTINGS_KEYS = frozenset({"host", "port", "username", "enabled"})


class ProxySettingsStore(Protocol):
    """The minimal persisted-proxy-configuration boundary, mirroring DeviceSettings."""

    def load(self) -> ProxySettings: ...

    def save(self, settings: ProxySettings) -> None: ...

    def clear(self) -> None: ...


@dataclass
class InMemoryProxySettings:
    """Non-persistent proxy settings used by injected local/test compositions."""

    settings: ProxySettings = field(default_factory=lambda: DEFAULT_PROXY_SETTINGS)

    def load(self) -> ProxySettings:
        return self.settings

    def save(self, settings: ProxySettings) -> None:
        self.settings = settings

    def clear(self) -> None:
        self.settings = DEFAULT_PROXY_SETTINGS


@dataclass
class InMemoryDeviceSettings:
    """Non-persistent settings used by injected local/test compositions."""

    selection: CaptureChoices | None = None

    def load(self) -> CaptureChoices | None:
        return self.selection

    def save(self, selection: CaptureChoices) -> None:
        self.selection = selection

    def clear(self) -> None:
        self.selection = None


class LocalDeviceSettings:
    """Persist only selected opaque device IDs below the current user's LocalAppData."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path or _local_app_data_path(_SETTINGS_FILENAME)

    def load(self) -> CaptureChoices | None:
        try:
            contents = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            payload = json.loads(contents)
        except json.JSONDecodeError:
            self.clear()
            return None
        if not isinstance(payload, dict) or set(payload) != _SELECTION_KEYS:
            self.clear()
            return None
        microphone_id = payload["microphone_id"]
        system_device_id = payload["system_device_id"]
        if (
            not isinstance(microphone_id, str)
            or not microphone_id
            or not isinstance(system_device_id, str)
            or not system_device_id
        ):
            self.clear()
            return None
        return CaptureChoices(microphone_id=microphone_id, system_device_id=system_device_id)

    def save(self, selection: CaptureChoices) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "microphone_id": selection.microphone_id,
                    "system_device_id": selection.system_device_id,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class LocalProxySettings:
    """Persist proxy connection settings -- host, port, username, enabled flag --
    below the current user's LocalAppData, alongside the device selection file.

    The password never reaches this file: it is saved separately, to
    CredentialStore, so a copy of this file alone never exposes a credential.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path or _local_app_data_path(_PROXY_SETTINGS_FILENAME)

    def load(self) -> ProxySettings:
        try:
            contents = self._path.read_text(encoding="utf-8")
        except OSError:
            return DEFAULT_PROXY_SETTINGS
        try:
            payload = json.loads(contents)
        except json.JSONDecodeError:
            self.clear()
            return DEFAULT_PROXY_SETTINGS
        if not isinstance(payload, dict) or set(payload) != _PROXY_SETTINGS_KEYS:
            self.clear()
            return DEFAULT_PROXY_SETTINGS
        host = payload["host"]
        port = payload["port"]
        username = payload["username"]
        enabled = payload["enabled"]
        if (
            not isinstance(host, str)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not isinstance(username, str)
            or not isinstance(enabled, bool)
        ):
            self.clear()
            return DEFAULT_PROXY_SETTINGS
        return ProxySettings(host=host, port=port, username=username, enabled=enabled)

    def save(self, settings: ProxySettings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "host": settings.host,
                    "port": settings.port,
                    "username": settings.username,
                    "enabled": settings.enabled,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _local_app_data_path(filename: str) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("Local application data is unavailable.")
    return Path(local_app_data) / _SETTINGS_DIRECTORY / filename
