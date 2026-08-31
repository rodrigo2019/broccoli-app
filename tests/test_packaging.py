from __future__ import annotations

from pathlib import Path

from broccoli_desktop.instance import PRODUCTION_INSTANCE_NAME

REPOSITORY_ROOT = Path(__file__).parent.parent


def test_the_spec_bundles_the_application_package_itself() -> None:
    """The packaged executable died at startup, on every build, with "Desktop
    runtime startup is not available yet."

    __main__ reaches the runtime through ``import_module("broccoli_desktop.
    runtime")`` -- deliberately, so the console script can report a missing
    runtime rather than fail to import. PyInstaller resolves imports by reading
    the source, and a name passed to import_module as a string is invisible to
    it, so nothing below __main__ was ever collected: the archive held
    broccoli_desktop and broccoli_desktop.config and stopped there.

    Nothing caught it because the build succeeds, the bundled tree carries every
    data file CI checks for, and the executable only fails when it is run --
    which no test and no CI job does.

    collect_submodules names them all, rather than listing broccoli_desktop.
    runtime alone, so a module reached only through a future dynamic import
    cannot reopen this.
    """
    specification = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.spec").read_text()

    assert 'collect_submodules("broccoli_desktop")' in specification


def test_the_spec_bundles_the_interface_catalogs() -> None:
    """The window is served by reading index.html and the catalogs off disk, so
    a locales directory left out of the archive fails at the first request --
    not at build time, which is the same shape as the two packaging defects
    that already shipped here."""
    specification = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.spec").read_text()

    assert '"broccoli_desktop/locales"' in specification


def test_main_still_reaches_the_runtime_the_way_the_spec_compensates_for() -> None:
    """The test above is only worth its weight while the dynamic import it
    describes is still there. If __main__ ever imports the runtime normally,
    PyInstaller finds it unaided and this pairing should be revisited rather
    than left as a rule nobody remembers the reason for."""
    entry_point = REPOSITORY_ROOT.joinpath("broccoli_desktop", "__main__.py").read_text()

    assert 'import_module("broccoli_desktop.runtime")' in entry_point


def test_ci_runs_the_startup_check_through_the_shared_script() -> None:
    """The check that the executable starts is the one thing that would have
    caught both packaging defects, and it is only useful if it is the same
    check locally and on CI. Inlining it in the workflow put it out of reach of
    anyone building a copy to hand around, which is exactly when it matters."""
    workflow = REPOSITORY_ROOT.joinpath(".github", "workflows", "ci.yml").read_text()
    script = REPOSITORY_ROOT.joinpath("scripts", "verify-package.ps1")

    assert script.is_file()
    assert r"scripts\verify-package.ps1" in workflow


def test_the_build_script_verifies_before_it_hands_over_an_archive() -> None:
    """A zip is produced to be given to someone else, so an unverified build
    must never reach the name someone would pick up and send on.

    The archive is written under a temporary name first -- compressing a tree
    the verification has already run would fail on the DLL handles Windows has
    not released yet -- so what matters is that the check happens before the
    rename, not before the compression.
    """
    build = REPOSITORY_ROOT.joinpath("scripts", "build-app.ps1").read_text()

    assert build.index("verify-package.ps1") < build.index("Move-Item")


def test_the_installer_reads_the_lock_the_running_application_holds() -> None:
    """Installing over a running copy overwrites files it still has open, and
    uninstalling one leaves its executable behind. Inno Setup asks the user to
    close the application instead -- but only for a mutex it knows the name of,
    and only while that name is still the one the application takes."""
    installer = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.iss").read_text()

    assert f"AppMutex={PRODUCTION_INSTANCE_NAME}" in installer


def test_the_spec_wires_the_splash_into_both_halves_of_the_onedir_build() -> None:
    """The splash is the only thing on screen for the seconds the frozen build
    spends importing pythonnet and PortAudio and waiting for WebView2, and it
    has a failure mode with no symptom at build time: ``EXE`` embeds the splash
    resource, ``COLLECT`` is what puts Tcl/Tk beside the executable for the
    bootloader to draw it with. Wire up only the first and PyInstaller reports
    success, the executable starts, and no splash ever appears.
    """
    specification = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.spec").read_text()

    assert "splash = Splash(" in specification
    assert specification.index("    splash,\n") > specification.index("exe = EXE(")
    assert specification.index("    splash.binaries,\n") > specification.index("coll = COLLECT(")


def test_the_splash_image_is_committed_rather_than_built_on_the_way_past() -> None:
    """PyInstaller reads the PNG while packaging and the bootloader draws it
    before the interpreter exists, so nothing at runtime can produce it. Like
    broccoli_icon.ico it is generated by hand -- scripts/build_splash.py -- and
    committed, and a build on a machine with no browser must still find it."""
    specification = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.spec").read_text()
    image = REPOSITORY_ROOT.joinpath("broccoli_desktop", "static", "images", "broccoli_splash.png")

    assert image.is_file()
    assert "broccoli_splash.png" in specification
    assert REPOSITORY_ROOT.joinpath("scripts", "build_splash.py").is_file()


def test_the_startup_check_asks_every_loopback_port_rather_than_the_first() -> None:
    """The splash screen made the packaged application listen on 127.0.0.1
    twice: the bootloader's Tcl interpreter opens an IPC socket before Python
    starts, and uvicorn binds its own port later.

    Taking the first port found therefore reached the splash, which accepts the
    connection and never answers, and the startup check failed against a build
    that was perfectly fine. Which port answers has to decide, not which one
    Windows happens to list first.
    """
    check = REPOSITORY_ROOT.joinpath("scripts", "verify-package.ps1").read_text()

    assert "$listening[0].LocalPort" not in check
    assert "foreach ($candidate in $ports)" in check
