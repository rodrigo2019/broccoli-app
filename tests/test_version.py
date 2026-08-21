from __future__ import annotations

import tomllib
from pathlib import Path

import broccoli_desktop


def test_package_exposes_a_version_matching_pyproject() -> None:
    """Hard-coding "0.1.0" made this fail on every version bump, which is a
    change detector, not a test."""
    manifest = tomllib.loads(Path(__file__).parent.parent.joinpath("pyproject.toml").read_text())

    assert broccoli_desktop.__version__ == manifest["project"]["version"]
