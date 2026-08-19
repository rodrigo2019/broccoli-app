from __future__ import annotations

import sys
import types

import pytest

from broccoli_desktop.config import PRODUCTION_SERVER_URL, parse_runtime_config
from broccoli_desktop.models import ConnectionState, SessionSummary, validate_title


def test_local_flag_overrides_the_embedded_production_url() -> None:
    config = parse_runtime_config(["--local"])

    assert config.server_url == "http://127.0.0.1:8000"
    assert config.environment == "local"


def test_dev_flag_uses_the_local_development_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROCCOLI_DESKTOP_DEV_URL", "https://desktop.dev.example")

    config = parse_runtime_config(["--dev"])

    assert config.server_url == "https://desktop.dev.example"
    assert config.environment == "development"


def test_default_config_uses_the_compiled_production_url() -> None:
    config = parse_runtime_config([])

    assert PRODUCTION_SERVER_URL == "https://broccoli.bosch-digital-factory.com"
    assert config.server_url == "https://broccoli.bosch-digital-factory.com"
    assert config.environment == "production"


def test_config_rejects_conflicting_local_and_dev_flags() -> None:
    with pytest.raises(SystemExit):
        parse_runtime_config(["--local", "--dev"])


def test_dev_flag_requires_a_development_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROCCOLI_DESKTOP_DEV_URL", raising=False)

    with pytest.raises(SystemExit):
        parse_runtime_config(["--dev"])


def test_main_parses_the_config_before_lazily_starting_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from broccoli_desktop import __main__

    received: list[object] = []
    runtime = types.ModuleType("broccoli_desktop.runtime")
    runtime.start = received.append
    monkeypatch.setitem(sys.modules, "broccoli_desktop.runtime", runtime)

    __main__.main(["--local"])

    assert received[0].server_url == "http://127.0.0.1:8000"


def test_main_reports_that_runtime_startup_is_not_available_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from broccoli_desktop import __main__

    monkeypatch.delitem(sys.modules, "broccoli_desktop.runtime", raising=False)

    with pytest.raises(RuntimeError, match="not available"):
        __main__.main([])


def test_main_uses_command_line_arguments_when_none_are_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from broccoli_desktop import __main__

    received: list[object] = []
    runtime = types.ModuleType("broccoli_desktop.runtime")
    runtime.start = received.append
    monkeypatch.setitem(sys.modules, "broccoli_desktop.runtime", runtime)
    monkeypatch.setattr(sys, "argv", ["broccoli-desktop", "--local"])

    __main__.main()

    assert received[0].environment == "local"


def test_title_validation_rejects_values_over_120_characters() -> None:
    with pytest.raises(ValueError, match="120"):
        validate_title("x" * 121)


def test_session_summary_is_immutable() -> None:
    summary = SessionSummary(
        uuid_code="session-1",
        title="Daily",
        status="active",
        started_at="2026-08-19T10:00:00Z",
        ended_at=None,
        device_label="Laptop",
        segment_count=0,
        is_live=True,
    )

    with pytest.raises(AttributeError):
        summary.status = ConnectionState.STOPPED
