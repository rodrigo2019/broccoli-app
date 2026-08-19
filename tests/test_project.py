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
    assert "ArchitecturesAllowed=x64compatible and not arm64" in installer
    assert "ArchitecturesInstallIn64BitMode=x64compatible and not arm64" in installer
    assert "MinVersion=10.0.22000" in installer
    assert "WebView2" in installer


def test_package_uses_explicit_project_root_pyinstaller_paths():
    package_script = Path("scripts/package.ps1").read_text(encoding="utf-8")

    assert '$buildRoot = Join-Path $projectRoot "build"' in package_script
    assert '$distRoot = Join-Path $projectRoot "dist"' in package_script
    assert "--workpath $buildRoot --distpath $distRoot" in package_script


def test_check_script_stops_after_each_failing_native_command():
    check_script = Path("scripts/check.ps1").read_text(encoding="utf-8")

    assert "function Invoke-NativeCommand" in check_script
    assert "if ($LASTEXITCODE -ne 0)" in check_script
    assert "& .\\.venv\\Scripts\\ruff.exe format --check ." in check_script
    assert "& .\\.venv\\Scripts\\ruff.exe check ." in check_script
    assert "& .\\.venv\\Scripts\\python.exe -m pytest" in check_script
