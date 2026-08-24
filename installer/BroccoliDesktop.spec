# ruff: noqa: F821
"""PyInstaller definition for the Windows-only Broccoli Desktop bundle."""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

PROJECT_ROOT = Path(SPECPATH).resolve().parent
#: The same file broccoli_desktop.branding hands to the window and the tray, so
#: the executable, its shortcuts, and the running application agree on one logo.
APPLICATION_ICON = PROJECT_ROOT / "broccoli_desktop" / "static" / "images" / "broccoli_icon.ico"
RUNTIME_PACKAGES = (
    "webview",
    "pyaudiowpatch",
    "soxr",
    "keyring",
    "pystray",
    "PIL",
)
WINDOWS_RUNTIME_IMPORTS = (
    "webview",
    "webview.guilib",
    "webview.platforms.winforms",
    "webview.platforms.win32",
    "webview.platforms.edgechromium",
    "clr",
    "clr_loader",
    "pyaudiowpatch",
    "_portaudiowpatch",
    "soxr",
    "keyring.backends.Windows",
    "pystray._win32",
    "PIL.Image",
)

datas = [(str(PROJECT_ROOT / "broccoli_desktop" / "static"), "broccoli_desktop/static")]
datas.extend(copy_metadata("keyring"))
binaries = []
# __main__ reaches the runtime through import_module(), whose argument is a
# string PyInstaller cannot follow -- so nothing below __main__ was collected
# and the executable died at startup on every build. Collecting the package
# names them all, which also covers whatever a later dynamic import reaches.
hiddenimports = list(WINDOWS_RUNTIME_IMPORTS) + collect_submodules("broccoli_desktop")
for package in RUNTIME_PACKAGES:
    datas.extend(collect_data_files(package))
    binaries.extend(collect_dynamic_libs(package))

a = Analysis(
    [str(PROJECT_ROOT / "broccoli_desktop" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BroccoliDesktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(APPLICATION_ICON),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BroccoliDesktop",
)
