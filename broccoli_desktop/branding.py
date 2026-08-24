"""How Windows recognizes this application: its logo, and its shell identity.

The window title bar, the notification area, ``BroccoliDesktop.exe`` and the
installer all want an ``.ico``; none of them read the SVG the web UI draws from.
``scripts/build_icon.py`` rasterizes one from that SVG, and this module is where
the rest of the application looks for the result, so the path is spelled once.

Owning the icon is not enough to put it on the taskbar, which is why the
AppUserModelID below lives here too -- see ``apply_taskbar_identity``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

logger = logging.getLogger(__name__)

#: Inside the static tree because PyInstaller already collects that directory
#: wholesale -- see the spec's ``datas``. A frozen build therefore resolves this
#: path the same way a source checkout does, with no ``sys._MEIPASS`` branch.
APPLICATION_ICON = Path(__file__).with_name("static") / "images" / "broccoli_icon.ico"

#: The identity the shell files this application's windows under. Microsoft's
#: format is Company.Product, at most 128 characters and no spaces. The
#: installer repeats it on the shortcuts it creates -- see the .iss -- so a
#: pinned shortcut and a running window are one taskbar button rather than two.
APPLICATION_IDENTITY = "Broccoli.BroccoliDesktop"


def apply_taskbar_identity() -> None:
    """Claim the identity the taskbar groups this application's windows under.

    The taskbar does not read the icon off the window. It resolves the window's
    AppUserModelID to an application and paints the button with *that*
    application's icon. Unset, the shell derives one from the running
    executable, so the button shows the icon of whatever launched the process --
    python.exe's terminal-and-snake in a source checkout -- however carefully
    the window sets its own.

    Must run before the first window exists: the shell reads the identity when
    it creates the taskbar button and does not revisit it afterwards.

    A failure here costs the button its logo and nothing else, so it is logged
    rather than raised: a cosmetic call must never be why the application
    refuses to open.
    """
    from ctypes import c_wchar_p, windll

    try:
        result = windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            c_wchar_p(APPLICATION_IDENTITY)
        )
    except Exception:
        logger.warning("Could not claim the taskbar identity for this process.", exc_info=True)
        return
    if result != 0:
        logger.warning(
            "The shell refused the taskbar identity %s (HRESULT 0x%08X); the taskbar "
            "button will show the launching executable's icon.",
            APPLICATION_IDENTITY,
            result & 0xFFFFFFFF,
        )


def load_icon_image() -> Image:
    """Read the logo as the in-memory image pystray hands to the tray.

    The largest frame, which is what Pillow returns for an ``.ico``: pystray
    re-encodes whatever it is given into a temporary icon file and lets Windows
    pick the frame that fits the notification area, so handing it the sharpest
    source is what keeps that choice from starting off an upscale.
    """
    from PIL import Image as PillowImage

    with PillowImage.open(APPLICATION_ICON) as icon:
        return icon.convert("RGBA")
