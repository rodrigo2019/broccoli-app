from __future__ import annotations

import re
import tomllib
from pathlib import Path

import broccoli_desktop

REPOSITORY_ROOT = Path(__file__).parent.parent


def project_version() -> str:
    manifest = tomllib.loads(REPOSITORY_ROOT.joinpath("pyproject.toml").read_text())
    return str(manifest["project"]["version"])


def test_package_exposes_a_version_matching_pyproject() -> None:
    """Hard-coding "0.1.0" made this fail on every version bump, which is a
    change detector, not a test."""
    assert broccoli_desktop.__version__ == project_version()


def test_the_installer_declares_the_project_version() -> None:
    """The version lives in four places, not one. The installer script is the
    one that decides what a user sees in Add/Remove Programs, and nothing tied
    it to pyproject -- a bump could ship a 0.2.0 build calling itself 0.1.0.

    Only the #define is checked because OutputBaseFilename now derives from it;
    that is asserted here too, so the file cannot quietly go back to spelling
    the number twice.
    """
    script = REPOSITORY_ROOT.joinpath("installer", "BroccoliDesktop.iss").read_text()
    declared = re.search(r'#define MyAppVersion "([^"]+)"', script)

    assert declared is not None
    assert declared.group(1) == project_version()
    assert "OutputBaseFilename=BroccoliDesktop-{#MyAppVersion}-setup" in script


def test_ci_uploads_the_installer_the_current_version_produces() -> None:
    """CI names the artifact path literally. Bump the version without touching
    this line and the packaging job fails on if-no-files-found: error -- after
    the whole build, for a reason that says nothing about the version."""
    workflow = REPOSITORY_ROOT.joinpath(".github", "workflows", "ci.yml").read_text()

    assert f"dist/installer/BroccoliDesktop-{project_version()}-setup.exe" in workflow
