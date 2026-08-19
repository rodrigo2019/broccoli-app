from importlib.metadata import version
from pathlib import Path


def test_package_exposes_the_initial_release_version():
    assert version("broccoli-desktop") == "0.1.0"


def test_pyinstaller_spec_collects_static_assets_and_audio_dependencies():
    spec = Path("installer/BroccoliDesktop.spec").read_text(encoding="utf-8")

    assert "broccoli_desktop/static" in spec
    assert "pyaudiowpatch" in spec
    assert "webview" in spec


def test_installer_declares_windows_11_x64_and_webview2_check():
    installer = Path("installer/BroccoliDesktop.iss").read_text(encoding="utf-8")

    assert "ArchitecturesAllowed=x64compatible" in installer
    assert "MinVersion=10.0.22000" in installer
    assert "WebView2" in installer
