from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from broccoli_desktop.branding import APPLICATION_ICON, APPLICATION_IDENTITY, load_icon_image

REPOSITORY_ROOT = Path(__file__).parent.parent
#: How the build scripts spell the icon's location: relative to the repository
#: root, with the separator Windows tooling writes.
ICON_SOURCE_PATH = str(APPLICATION_ICON.relative_to(REPOSITORY_ROOT)).replace("/", "\\")


def test_the_application_icon_ships_inside_the_packaged_tree() -> None:
    """Every Windows surface reads one file: the window, the tray, the
    executable, and the installer. It lives under static/ because that is the
    directory PyInstaller already bundles, so the frozen build resolves this
    path with no _MEIPASS special case."""
    assert APPLICATION_ICON.is_file()
    assert APPLICATION_ICON.is_relative_to(REPOSITORY_ROOT / "broccoli_desktop" / "static")


def test_the_application_icon_carries_the_sizes_windows_picks_between() -> None:
    """Windows chooses a frame per surface rather than scaling one: 16 for the
    notification area and the title bar, 32 for Alt+Tab and the shortcut, 256
    for Explorer's largest view. A missing frame is upscaled from whatever is
    nearest, which is where an icon starts looking blurred."""
    with Image.open(APPLICATION_ICON) as icon:
        sizes = icon.info["sizes"]

    assert {(16, 16), (32, 32), (256, 256)} <= sizes


def test_the_tray_image_is_the_logo_rather_than_a_flat_fill() -> None:
    """The tray shipped a solid green square -- Image.new("RGBA", (64, 64),
    "#1f8b4c") -- which is a placeholder that reads as a rendering fault, not
    as an application. A logo covers part of its canvas in more than one
    colour; a flat fill covers all of it in exactly one."""
    image = load_icon_image()

    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0
    assert image.getcolors(maxcolors=2) is None


def test_the_packaged_executable_is_built_with_the_shipped_icon() -> None:
    """The spec pointed PyInstaller at its own bootloader icon, so the built
    exe -- and with it the taskbar, Alt+Tab, and both shortcuts the installer
    creates -- carried PyInstaller's default artwork instead of the product's.
    """
    specification = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.spec").read_text()

    assert "icon-windowed.ico" not in specification
    assert APPLICATION_ICON.name in specification


def test_the_installer_is_stamped_with_the_shipped_icon() -> None:
    """Inno Setup falls back to its own icon for setup.exe, which is the first
    thing a user sees of this application and the one file they download."""
    script = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.iss").read_text()

    assert f"SetupIconFile={{#SourcePath}}\\..\\{ICON_SOURCE_PATH}" in script


def test_ci_verifies_the_packaged_tree_carries_the_icon() -> None:
    """The icon is a data file, so a spec that stops collecting static/ breaks
    the window and the tray without failing the build. CI already asserts the
    other bundled assets exist; the icon belongs on that list."""
    workflow = REPOSITORY_ROOT.joinpath(".github", "workflows", "ci.yml").read_text()

    assert f'"static/images/{APPLICATION_ICON.name}"' in workflow


def test_the_installer_shortcuts_share_the_running_applications_identity() -> None:
    """The shell groups taskbar buttons by AppUserModelID and pins shortcuts by
    it. If the shortcuts the installer creates disagree with what the process
    claims at startup, pinning one of them produces a second button that never
    lights up while the application it points at is running."""
    script = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.iss").read_text()
    declared = re.search(r'#define MyAppUserModelId "([^"]+)"', script)

    assert declared is not None
    assert declared.group(1) == APPLICATION_IDENTITY
    assert script.count('AppUserModelID: "{#MyAppUserModelId}"') == script.count('Name: "{auto')
