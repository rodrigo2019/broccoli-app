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
