"""Local storage for the selected devices, the proxy route and the interface language."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from broccoli_desktop.i18n import SUPPORTED_UI_LOCALES
from broccoli_desktop.session import CaptureChoices

_SETTINGS_DIRECTORY = "Broccoli Desktop"
_SETTINGS_FILENAME = "device-selections.json"
_SELECTION_KEYS = frozenset({"microphone_id", "system_device_id"})


class DeviceSettings(Protocol):
    """The minimal persisted-selection boundary used by local application services."""

    def load(self) -> CaptureChoices | None: ...

    def save(self, selection: CaptureChoices) -> None: ...

    def clear(self) -> None: ...


class ProxyMode(StrEnum):
    """Which of the two ways of naming a proxy the user chose.

    Mirrors the choice Windows itself offers under Settings > Network &
    Internet > Proxy, because that is where anyone on a corporate network has
    already met it.
    """

    #: A host and port typed by hand.
    MANUAL = "manual"
    #: An address for an automatic configuration script (PAC), which names the
    #: proxy per destination. See broccoli_desktop.autoproxy.
    SCRIPT = "script"


#: Offered in the settings screen so nobody on the corporate network has to go
#: and find it. Only ever a starting value: the proxy stays disabled until
#: somebody switches it on, so an install outside that network never reaches for
#: this address on its own. The product already embeds its backend hostnames the
#: same way -- see the README.
DEFAULT_SCRIPT_URL = "http://rbins.bosch.com/ca.pac"


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
    mode: ProxyMode = ProxyMode.MANUAL
    script_url: str = DEFAULT_SCRIPT_URL


DEFAULT_PROXY_SETTINGS = ProxySettings()

_PROXY_SETTINGS_FILENAME = "proxy-settings.json"
_PROXY_SETTINGS_KEYS = frozenset({"host", "port", "username", "enabled"})
#: Added after the first release. A file written before they existed is a valid
#: file, not a corrupt one -- see LocalProxySettings.load.
_OPTIONAL_PROXY_SETTINGS_KEYS = frozenset({"mode", "script_url"})


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
        # Required keys must all be present and nothing unknown may appear, but
        # the keys added after the first release are optional: a file written
        # by an earlier build is complete for what that build knew, and
        # answering it with clear() would delete a working proxy configuration
        # on exactly the network where the app cannot reach the backend without
        # one.
        if not isinstance(payload, dict):
            self.clear()
            return DEFAULT_PROXY_SETTINGS
        keys = set(payload)
        if not _PROXY_SETTINGS_KEYS <= keys or not keys <= (
            _PROXY_SETTINGS_KEYS | _OPTIONAL_PROXY_SETTINGS_KEYS
        ):
            self.clear()
            return DEFAULT_PROXY_SETTINGS
        host = payload["host"]
        port = payload["port"]
        username = payload["username"]
        enabled = payload["enabled"]
        mode = payload.get("mode", ProxyMode.MANUAL.value)
        script_url = payload.get("script_url", DEFAULT_SCRIPT_URL)
        if (
            not isinstance(host, str)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not isinstance(username, str)
            or not isinstance(enabled, bool)
            or not isinstance(script_url, str)
            or mode not in frozenset(ProxyMode)
        ):
            # An unrecognised mode is refused rather than downgraded to manual:
            # routing through whatever host happens to sit in the same file is
            # not what the file asks for.
            self.clear()
            return DEFAULT_PROXY_SETTINGS
        return ProxySettings(
            host=host,
            port=port,
            username=username,
            enabled=enabled,
            mode=ProxyMode(mode),
            script_url=script_url,
        )

    def save(self, settings: ProxySettings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(
                {
                    "host": settings.host,
                    "port": settings.port,
                    "username": settings.username,
                    "enabled": settings.enabled,
                    "mode": settings.mode.value,
                    "script_url": settings.script_url,
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


_UI_SETTINGS_FILENAME = "ui-settings.json"
_UI_SETTINGS_KEYS = frozenset({"locale"})


class UiSettingsStore(Protocol):
    """The stored interface language, mirroring the other two settings boundaries.

    ``load`` answers ``None`` rather than a default locale: absent means nobody
    has chosen, which is what tells broccoli_desktop.i18n to ask Windows.
    """

    def load(self) -> str | None: ...

    def save(self, locale: str) -> None: ...

    def clear(self) -> None: ...


@dataclass
class InMemoryUiSettings:
    """Non-persistent interface language used by injected local/test compositions."""

    locale: str | None = None

    def load(self) -> str | None:
        return self.locale

    def save(self, locale: str) -> None:
        self.locale = locale

    def clear(self) -> None:
        self.locale = None


class LocalUiSettings:
    """Persist the chosen interface language beside the other selection files.

    The value is memoised because the notification-area menu asks for it every
    time Windows paints that menu, and a disk read per paint is not what a tray
    label should cost. Writes go to both the memo and the file, and the memo is
    a single ``str`` assignment -- atomic under the GIL -- which is what makes
    the hand-off safe from the server thread that saves it to the tray thread
    that reads it, with no lock between them.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path or _local_app_data_path(_UI_SETTINGS_FILENAME)
        self._memo: str | None = None
        self._loaded = False

    def load(self) -> str | None:
        if self._loaded:
            return self._memo
        self._memo = self._read()
        self._loaded = True
        return self._memo

    def _read(self) -> str | None:
        try:
            contents = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(contents)
        except json.JSONDecodeError:
            self._discard()
            return None
        if not isinstance(payload, dict) or set(payload) != _UI_SETTINGS_KEYS:
            self._discard()
            return None
        locale = payload["locale"]
        # An unrecognised locale is discarded rather than kept: it can only come
        # from a hand-edited file or a downgrade, and it must never be handed on
        # as a filename (see broccoli_desktop.i18n's catalog table).
        if locale not in SUPPORTED_UI_LOCALES:
            self._discard()
            return None
        return str(locale)

    def save(self, locale: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"locale": locale}, separators=(",", ":")),
            encoding="utf-8",
        )
        self._memo = locale
        self._loaded = True

    def clear(self) -> None:
        self._discard()
        self._memo = None
        self._loaded = True

    def _discard(self) -> None:
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
