from __future__ import annotations

import json
from pathlib import Path

import pytest

from broccoli_desktop.i18n import (
    DEFAULT_UI_LOCALE,
    LOCALES_DIRECTORY,
    SUPPORTED_UI_LOCALES,
    Translator,
    all_catalogs,
    load_catalog,
    match_locale,
    resolve_locale,
    translate,
)
from broccoli_desktop.settings import InMemoryUiSettings, LocalUiSettings


def test_every_catalog_answers_exactly_the_same_keys() -> None:
    """A key one language has and another does not renders as the key itself.

    Comparing the sets rather than checking each file for "enough" entries is
    what catches the shape this actually takes: a string added in the language
    whoever wrote it was thinking in, and nowhere else.
    """
    catalogs = {locale: load_catalog(locale) for locale in SUPPORTED_UI_LOCALES}
    reference = catalogs[DEFAULT_UI_LOCALE]

    assert len(reference["ui"]) >= 150
    for locale, catalog in catalogs.items():
        assert set(catalog["ui"]) == set(reference["ui"]), locale
        assert set(catalog["api"]) == set(reference["api"]), locale


def test_no_catalog_entry_is_blank() -> None:
    """An empty string passes a key-set comparison and renders as an empty
    button, which is the one failure mode worse than the untranslated word."""
    for locale in SUPPORTED_UI_LOCALES:
        catalog = load_catalog(locale)
        for section in ("ui", "api"):
            blank = sorted(key for key, value in catalog[section].items() if not value.strip())
            assert not blank, (locale, section, blank)


def test_the_placeholders_of_a_message_are_the_same_in_every_language() -> None:
    """`{label}` dropped from a translation is a name that silently loses the
    thing it was naming -- "Options for" with nothing after it."""
    reference = load_catalog(DEFAULT_UI_LOCALE)["ui"]

    for locale in SUPPORTED_UI_LOCALES:
        catalog = load_catalog(locale)["ui"]
        for key, message in reference.items():
            expected = {part.split("}")[0] for part in message.split("{")[1:]}
            actual = {part.split("}")[0] for part in catalog[key].split("{")[1:]}
            assert actual == expected, (locale, key)


def test_a_catalog_file_is_json_with_only_the_two_sections() -> None:
    """The files are the committed source of truth, so their shape is part of
    the contract: two flat maps, no nesting for a lookup to walk."""
    for locale in SUPPORTED_UI_LOCALES:
        payload = json.loads((LOCALES_DIRECTORY / f"{locale}.json").read_text(encoding="utf-8"))
        assert set(payload) == {"ui", "api"}
        for section in payload.values():
            assert all(isinstance(value, str) for value in section.values())


def test_all_catalogs_carries_every_language() -> None:
    """What the served shell embeds, so a language change needs no request."""
    assert set(all_catalogs()) == set(SUPPORTED_UI_LOCALES)


def test_a_stored_choice_beats_whatever_windows_is_set_to() -> None:
    """The point of the setting: someone on a German machine who wants the
    application in English has said so, and Windows does not get a vote."""
    assert resolve_locale("en", preferred=lambda: ("de-DE",)) == "en"
    assert resolve_locale("pt-BR", preferred=lambda: ("de-DE",)) == "pt-BR"


def test_with_nothing_stored_the_windows_display_language_decides() -> None:
    assert resolve_locale(None, preferred=lambda: ("de-DE", "en-US")) == "de"
    assert resolve_locale(None, preferred=lambda: ("en-GB",)) == "en"


def test_a_regional_variant_lands_on_the_language_that_is_shipped() -> None:
    """pt-PT is not pt-BR, but Portuguese is far closer to what that user asked
    for than the English fallback."""
    assert resolve_locale(None, preferred=lambda: ("pt-PT",)) == "pt-BR"
    assert resolve_locale(None, preferred=lambda: ("de-AT",)) == "de"


def test_an_exact_match_further_down_the_list_beats_a_near_match_at_the_top() -> None:
    """The list is ordered by preference, so a machine asking for Portuguese
    first and English second must not be answered in English."""
    assert match_locale(("pt-PT", "en-US")) == "pt-BR"
    assert match_locale(("es-ES", "en-US")) == "en"


def test_nothing_recognisable_falls_back_to_english() -> None:
    assert resolve_locale(None, preferred=lambda: ()) == DEFAULT_UI_LOCALE
    assert resolve_locale(None, preferred=lambda: ("ja-JP", "ko-KR")) == DEFAULT_UI_LOCALE
    assert match_locale(("ja-JP",)) is None


def test_a_stored_locale_can_never_become_a_path() -> None:
    """The stored value comes off a JSON file a user can edit, and the only
    thing standing between it and open() is this refusal."""
    assert resolve_locale("../../../etc/passwd") == DEFAULT_UI_LOCALE

    with pytest.raises(ValueError):
        load_catalog("../../../etc/passwd")


def test_a_missing_key_falls_back_through_english_and_then_to_itself() -> None:
    """Never blank: the key is a poor label and a far better one than nothing,
    and it is what the visual gate collects to prove none are reaching a
    screen."""
    assert translate("de", "settings.title") == load_catalog("de")["ui"]["settings.title"]
    assert translate("de", "no.such.key") == "no.such.key"


def test_a_message_substitutes_only_when_it_is_given_something_to_substitute() -> None:
    assert translate("en", "tray.status", status="Ready") == "Status: Ready"
    # Asking for a plain message must not run it through format(), or a literal
    # brace in a translation would raise instead of rendering.
    assert "{" not in translate("en", "settings.title")


def test_the_translator_re_reads_the_store_on_every_call() -> None:
    """The notification-area menu is painted long after it is built, so a
    translator that captured its locale at construction would leave the tray
    speaking the language the application started in."""
    settings = InMemoryUiSettings("pt-BR")
    translator = Translator(settings)

    assert translator.locale == "pt-BR"
    before = translator.t("tray.quit")

    settings.save("de")

    assert translator.locale == "de"
    assert translator.t("tray.quit") != before


def test_the_stored_language_survives_a_round_trip_to_disk(tmp_path: Path) -> None:
    store = LocalUiSettings(path=tmp_path / "ui-settings.json")

    assert store.load() is None

    store.save("de")

    assert store.load() == "de"
    assert LocalUiSettings(path=tmp_path / "ui-settings.json").load() == "de"


def test_clearing_the_language_goes_back_to_following_windows(tmp_path: Path) -> None:
    """None, not a default locale: absent is what tells resolve_locale to ask
    Windows, so "Follow Windows" has to leave nothing behind."""
    path = tmp_path / "ui-settings.json"
    store = LocalUiSettings(path=path)
    store.save("de")

    store.clear()

    assert store.load() is None
    assert not path.exists()
    assert LocalUiSettings(path=path).load() is None


@pytest.mark.parametrize(
    "contents",
    ["not json at all", '{"locale": "klingon"}', '{"locale": 5}', '{"locale": "de", "extra": 1}'],
)
def test_an_unusable_language_file_is_discarded_rather_than_carried(
    tmp_path: Path, contents: str
) -> None:
    """Same contract as the proxy and device files beside it. An unrecognised
    locale is discarded rather than kept, because the next thing that happens
    to it is a catalog lookup keyed by filename."""
    path = tmp_path / "ui-settings.json"
    path.write_text(contents, encoding="utf-8")

    assert LocalUiSettings(path=path).load() is None
    assert not path.exists()


def test_an_unreadable_language_file_leaves_the_choice_unmade(tmp_path: Path) -> None:
    """A directory where the file should be: unreadable, but not evidence of
    anything, so it is answered the same way a missing file is."""
    path = tmp_path / "ui-settings.json"
    path.mkdir()

    assert LocalUiSettings(path=path).load() is None
    assert path.is_dir()
