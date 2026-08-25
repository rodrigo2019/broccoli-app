"""Interface language: the catalogs, the Windows detection, and the lookup.

One catalog per locale, read from ``locales/*.json`` and shared by all three
surfaces that speak to the user: this process (the notification-area menu and
the Windows dialogs), the shell served at ``/``, and the script running inside
the window. A second copy of the same wording would be a second copy to keep in
step, which is how a translated application ends up half translated.

Each catalog holds two flat maps rather than one nested tree. ``ui`` is keyed by
a semantic name; ``api`` is keyed by the *English* ``ApiError`` detail the
backend sends, which is a wire contract and contains dots of its own -- keeping
the two apart is what lets both be a plain dictionary index instead of a path
walk, and it is what lets the guard in tests/test_api.py compare key sets
directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from functools import cache, lru_cache
from pathlib import Path
from typing import Protocol

#: The interface languages the product ships. Ordered as they are offered.
SUPPORTED_UI_LOCALES: tuple[str, ...] = ("en", "pt-BR", "de")

#: Where every lookup lands when nothing else matches, and the locale whose
#: catalog every other catalog falls back to key by key.
DEFAULT_UI_LOCALE = "en"

LOCALES_DIRECTORY = Path(__file__).with_name("locales")

#: An explicit map rather than f"{locale}.json". The stored locale arrives from
#: a JSON file on disk that a user can edit, so a value like "../../../x" must
#: never reach the filesystem -- and a table cannot be talked into a path.
_CATALOG_FILENAMES: dict[str, str] = {
    "en": "en.json",
    "pt-BR": "pt-BR.json",
    "de": "de.json",
}

_UI_SECTION = "ui"
_API_SECTION = "api"


class LocaleSettings(Protocol):
    """The stored-locale boundary, as small as the tray and the dialogs need."""

    def load(self) -> str | None: ...


@cache
def load_catalog(locale: str) -> dict[str, dict[str, str]]:
    """Read one catalog, cached for the life of the process.

    Raises for an unsupported locale instead of falling back: every caller here
    has already been through ``resolve_locale``, so an unknown value at this
    point is a defect rather than a preference.
    """
    try:
        filename = _CATALOG_FILENAMES[locale]
    except KeyError:
        raise ValueError(f"{locale!r} is not a supported interface locale") from None
    payload = json.loads((LOCALES_DIRECTORY / filename).read_text(encoding="utf-8"))
    return {
        _UI_SECTION: dict(payload.get(_UI_SECTION, {})),
        _API_SECTION: dict(payload.get(_API_SECTION, {})),
    }


@lru_cache(maxsize=1)
def all_catalogs() -> dict[str, dict[str, dict[str, str]]]:
    """Every catalog, as served into the window so a language change is instant."""
    return {locale: load_catalog(locale) for locale in SUPPORTED_UI_LOCALES}


def translate(locale: str, key: str, **params: object) -> str:
    """Look one key up, falling back through English to the key itself.

    Returning the key rather than an empty string keeps a missing entry visible
    in the window instead of silently blanking a button.
    """
    for candidate in (locale, DEFAULT_UI_LOCALE):
        if candidate not in _CATALOG_FILENAMES:
            continue
        message = load_catalog(candidate)[_UI_SECTION].get(key)
        if message:
            # format() only when there is something to substitute, so a literal
            # brace in a translation is not a formatting error.
            return message.format(**params) if params else message
    return key


#: MUI_LANGUAGE_NAME -- ask for BCP 47 names rather than LCIDs.
_MUI_LANGUAGE_NAME = 0x8


def _windows_preferred_ui_languages() -> tuple[str, ...]:
    """The user's Windows *display* languages, most preferred first.

    ``GetUserPreferredUILanguages`` is the only one of the obvious candidates
    that reads the right setting. ``locale.getlocale`` and
    ``GetUserDefaultLocaleName`` report the *format* locale, which is a
    separate choice in Windows -- an English install formatting dates the
    Brazilian way is a normal configuration, and it must not flip the interface
    to Portuguese. ``GetUserDefaultUILanguage`` reads the right setting but
    answers with a bare LCID, which would need a hand-kept table here.

    Empty when the call is unavailable, which is what sends resolve_locale to
    its default rather than raising on a machine that is not Windows.
    """
    try:
        from ctypes import POINTER, byref, create_unicode_buffer, windll
        from ctypes.wintypes import BOOL, DWORD, LPWSTR, ULONG
    except (ImportError, AttributeError):
        return ()
    try:
        get_languages = windll.kernel32.GetUserPreferredUILanguages
        get_languages.argtypes = [DWORD, POINTER(ULONG), LPWSTR, POINTER(ULONG)]
        get_languages.restype = BOOL
        count = ULONG()
        size = ULONG()
        # Called twice, the way the Win32 buffer protocol asks: once with no
        # buffer to learn the size, then once to fill it.
        if not get_languages(_MUI_LANGUAGE_NAME, byref(count), None, byref(size)):
            return ()
        buffer = create_unicode_buffer(size.value)
        if not get_languages(_MUI_LANGUAGE_NAME, byref(count), buffer, byref(size)):
            return ()
    except (OSError, AttributeError, ValueError, TypeError):
        return ()
    # A double-null-terminated block of names: "de-DE\0en-US\0\0".
    return tuple(tag for tag in buffer[: size.value].split("\0") if tag)


def match_locale(preferred: Iterable[str]) -> str | None:
    """The first offered locale that a preference list asks for, or None.

    Exact match first across the whole list, then primary subtag, so a machine
    listing ``pt-PT, en-US`` is answered with Portuguese rather than with
    English on a technicality.
    """
    tags = [tag.strip() for tag in preferred if tag and tag.strip()]
    folded = {locale.casefold(): locale for locale in SUPPORTED_UI_LOCALES}
    for tag in tags:
        match = folded.get(tag.casefold())
        if match:
            return match
    primary = {locale.split("-")[0].casefold(): locale for locale in reversed(SUPPORTED_UI_LOCALES)}
    for tag in tags:
        match = primary.get(tag.split("-")[0].casefold())
        if match:
            return match
    return None


def resolve_locale(
    stored: str | None,
    *,
    preferred: Callable[[], Iterable[str]] = _windows_preferred_ui_languages,
) -> str:
    """Which language the application speaks right now.

    An explicit choice always wins. With none stored -- a fresh install, or the
    "follow Windows" option -- the Windows display language decides, and
    English is the answer when it names nothing we ship.

    ``preferred`` is injected for the same reason ``proxy_prober`` and
    ``script_resolver`` are: no test should have to reach for ctypes.
    """
    if stored in SUPPORTED_UI_LOCALES:
        return str(stored)
    return match_locale(preferred()) or DEFAULT_UI_LOCALE


class Translator:
    """Translate against whatever locale is stored *at the moment of the call*.

    The notification-area menu and the Windows dialogs are painted long after
    they are built, so they cannot capture a locale at construction: reading it
    per call is what lets a language chosen in the settings screen reach them
    without a restart.
    """

    def __init__(self, settings: LocaleSettings) -> None:
        self._settings = settings

    @property
    def locale(self) -> str:
        return resolve_locale(self._settings.load())

    def t(self, key: str, **params: object) -> str:
        return translate(self.locale, key, **params)
