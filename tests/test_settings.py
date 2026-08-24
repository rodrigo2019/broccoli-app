from __future__ import annotations

import json


def test_local_device_settings_store_only_the_two_opaque_device_ids(tmp_path) -> None:
    from broccoli_desktop.session import CaptureChoices
    from broccoli_desktop.settings import LocalDeviceSettings

    path = tmp_path / "device-selections.json"
    store = LocalDeviceSettings(path=path)
    selection = CaptureChoices("mic-opaque-id", "system-opaque-id")

    store.save(selection)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "microphone_id": "mic-opaque-id",
        "system_device_id": "system-opaque-id",
    }
    assert store.load() == selection


def test_local_proxy_settings_round_trip_without_a_password_field(tmp_path) -> None:
    """ProxySettings has no password attribute -- this asserts the file on disk
    can never carry one, however the shape is extended in the future."""
    from broccoli_desktop.settings import (
        DEFAULT_SCRIPT_URL,
        LocalProxySettings,
        ProxySettings,
    )

    path = tmp_path / "proxy-settings.json"
    store = LocalProxySettings(path=path)
    settings = ProxySettings(host="proxy.local", port=8080, username="user", enabled=True)

    store.save(settings)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert stored == {
        "host": "proxy.local",
        "port": 8080,
        "username": "user",
        "enabled": True,
        "mode": "manual",
        "script_url": DEFAULT_SCRIPT_URL,
    }
    assert "password" not in stored
    assert store.load() == settings


def test_local_proxy_settings_default_to_disabled_when_nothing_was_saved(tmp_path) -> None:
    from broccoli_desktop.settings import DEFAULT_PROXY_SETTINGS, LocalProxySettings

    store = LocalProxySettings(path=tmp_path / "proxy-settings.json")

    assert store.load() == DEFAULT_PROXY_SETTINGS
    assert DEFAULT_PROXY_SETTINGS.enabled is False


def test_local_proxy_settings_clear_removes_the_file(tmp_path) -> None:
    from broccoli_desktop.settings import DEFAULT_PROXY_SETTINGS, LocalProxySettings, ProxySettings

    path = tmp_path / "proxy-settings.json"
    store = LocalProxySettings(path=path)
    store.save(ProxySettings(host="proxy.local", port=8080, username="user", enabled=True))

    store.clear()

    assert not path.exists()
    assert store.load() == DEFAULT_PROXY_SETTINGS


def test_local_proxy_settings_round_trip_an_automatic_configuration_script(tmp_path) -> None:
    """The script address is a connection setting like the host and the port,
    and belongs in the same file -- not least because it must survive a restart
    for the app to reach the backend at all on a network that requires it."""
    from broccoli_desktop.settings import LocalProxySettings, ProxyMode, ProxySettings

    path = tmp_path / "proxy-settings.json"
    store = LocalProxySettings(path=path)
    settings = ProxySettings(
        enabled=True,
        mode=ProxyMode.SCRIPT,
        script_url="http://proxy.local/config.pac",
        username="user",
    )

    store.save(settings)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert stored["mode"] == "script"
    assert stored["script_url"] == "http://proxy.local/config.pac"
    assert "password" not in stored
    assert store.load() == settings


def test_a_proxy_file_written_before_scripts_existed_still_loads(tmp_path) -> None:
    """load() used to demand the file's keys equal the expected set exactly, so
    adding these two would have made every already-configured user's file
    "invalid" -- and load() answers that by deleting it. Someone who had set up
    a corporate proxy would have found it silently gone after an update, on the
    very network where they cannot reach the backend without it."""
    from broccoli_desktop.settings import LocalProxySettings, ProxyMode

    path = tmp_path / "proxy-settings.json"
    path.write_text(
        json.dumps({"host": "proxy.local", "port": 8080, "username": "user", "enabled": True}),
        encoding="utf-8",
    )

    settings = LocalProxySettings(path=path).load()

    assert settings.host == "proxy.local"
    assert settings.port == 8080
    assert settings.enabled is True
    assert settings.mode == ProxyMode.MANUAL
    assert path.exists()


def test_a_proxy_file_naming_an_unknown_mode_is_refused(tmp_path) -> None:
    """A mode this build does not understand cannot be routed through. Falling
    back to manual would connect through whatever host happens to be in the same
    file, which is not what the file asks for."""
    from broccoli_desktop.settings import DEFAULT_PROXY_SETTINGS, LocalProxySettings

    path = tmp_path / "proxy-settings.json"
    path.write_text(
        json.dumps(
            {
                "host": "",
                "port": 0,
                "username": "",
                "enabled": True,
                "mode": "telepathy",
                "script_url": "",
            }
        ),
        encoding="utf-8",
    )

    assert LocalProxySettings(path=path).load() == DEFAULT_PROXY_SETTINGS


def test_the_default_script_address_is_offered_but_not_switched_on(tmp_path) -> None:
    """The field arrives filled in so nobody on the corporate network has to
    hunt for the address, but nothing is routed anywhere until someone enables
    it: a default that reached the network on first launch would be a surprise
    for every install outside that network."""
    from broccoli_desktop.settings import DEFAULT_PROXY_SETTINGS, DEFAULT_SCRIPT_URL

    assert DEFAULT_PROXY_SETTINGS.script_url == DEFAULT_SCRIPT_URL
    assert DEFAULT_SCRIPT_URL == "http://rbins.bosch.com/ca.pac"
    assert DEFAULT_PROXY_SETTINGS.enabled is False
