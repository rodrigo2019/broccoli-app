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
    from broccoli_desktop.settings import LocalProxySettings, ProxySettings

    path = tmp_path / "proxy-settings.json"
    store = LocalProxySettings(path=path)
    settings = ProxySettings(host="proxy.local", port=8080, username="user", enabled=True)

    store.save(settings)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert stored == {"host": "proxy.local", "port": 8080, "username": "user", "enabled": True}
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
