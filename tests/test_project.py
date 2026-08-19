from importlib.metadata import version


def test_package_exposes_the_initial_release_version():
    assert version("broccoli-desktop") == "0.1.0"
