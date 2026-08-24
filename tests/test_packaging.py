from __future__ import annotations

from pathlib import Path

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


def test_main_still_reaches_the_runtime_the_way_the_spec_compensates_for() -> None:
    """The test above is only worth its weight while the dynamic import it
    describes is still there. If __main__ ever imports the runtime normally,
    PyInstaller finds it unaided and this pairing should be revisited rather
    than left as a rule nobody remembers the reason for."""
    entry_point = REPOSITORY_ROOT.joinpath("broccoli_desktop", "__main__.py").read_text()

    assert 'import_module("broccoli_desktop.runtime")' in entry_point
