r"""Rasterize the Broccoli logo into the Windows icon the desktop bundle ships.

The application icon has to exist as a real ``.ico`` file: PyInstaller embeds
one in ``BroccoliDesktop.exe``, Inno Setup stamps one on the installer, and
WinForms loads one through ``System.Drawing.Icon`` for the window. None of them
read SVG, so the committed ``broccoli_icon.ico`` is generated from
``broccoli_icon.svg`` here rather than hand-drawn -- rerun this script whenever
the logo changes, then commit the result.

The rasterizer is whichever Chromium browser the machine already has, which
keeps a headless SVG renderer out of the project's dependencies. Not every
install answers a headless screenshot request -- Edge on some Windows 11
machines writes nothing at all -- so the candidates are tried in turn until one
produces a file.

Frames are written as bitmaps rather than PNG: ``System.Drawing.Icon`` is the
oldest consumer in the list and the one least willing to decode a
PNG-compressed frame.

    .venv/Scripts/python.exe scripts/build_icon.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIRECTORY = PROJECT_ROOT / "broccoli_desktop" / "static" / "images"
SOURCE_LOGO = IMAGES_DIRECTORY / "broccoli_icon.svg"
TARGET_ICON = IMAGES_DIRECTORY / "broccoli_icon.ico"

#: Rendered once at this edge, then downsampled per frame. Larger than the
#: biggest frame so every size below it is a reduction rather than an upscale.
MASTER_EDGE = 1024
#: The sizes Windows selects between: the tray and title bar take the smallest,
#: Alt+Tab and the shortcut the middle, Explorer's largest view the top one.
ICON_SIZES = ((16, 16), (20, 20), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256))
BROWSER_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)

PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{ width: {edge}px; height: {edge}px; display: flex;
          align-items: center; justify-content: center; }}
  svg {{ width: auto; height: {edge}px; }}
</style></head><body>{logo}</body></html>
"""


def write_page(workspace: Path) -> Path:
    """Lay the logo out on a transparent square canvas the screenshot matches."""
    page = workspace / "logo.html"
    page.write_text(
        PAGE_TEMPLATE.format(edge=MASTER_EDGE, logo=SOURCE_LOGO.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    return page


def render_master(workspace: Path) -> Path:
    """Screenshot the logo page with the first browser that manages to."""
    page = write_page(workspace)
    installed = [candidate for candidate in BROWSER_CANDIDATES if candidate.is_file()]
    if not installed:
        raise RuntimeError("No Chrome or Edge installation was found to render the logo.")
    for index, browser in enumerate(installed):
        master = workspace / f"logo-{index}.png"
        subprocess.run(
            [
                str(browser),
                "--headless",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                "--default-background-color=00000000",
                f"--screenshot={master}",
                f"--window-size={MASTER_EDGE},{MASTER_EDGE}",
                page.as_uri(),
            ],
            check=False,
            capture_output=True,
        )
        if master.is_file():
            return master
    attempted = ", ".join(browser.name for browser in installed)
    raise RuntimeError(f"No installed browser wrote a screenshot of the logo (tried {attempted}).")


def build_icon() -> None:
    with tempfile.TemporaryDirectory(prefix="broccoli-icon-") as directory:
        master = Image.open(render_master(Path(directory))).convert("RGBA")
        if master.getchannel("A").getextrema()[0] != 0:
            raise RuntimeError("The rendered logo has no transparent background to cut out.")
        master.save(TARGET_ICON, format="ICO", sizes=ICON_SIZES, bitmap_format="bmp")
    print(f"Wrote {TARGET_ICON.relative_to(PROJECT_ROOT)} at {len(ICON_SIZES)} sizes.")


if __name__ == "__main__":
    try:
        build_icon()
    except RuntimeError as error:
        sys.exit(str(error))
