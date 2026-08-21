from __future__ import annotations

import sys
import types

import pytest

from broccoli_desktop.config import (
    DEVELOPMENT_SERVER_URL,
    PRODUCTION_SERVER_URL,
    WEBSOCKET_PATH,
    parse_runtime_config,
)
from broccoli_desktop.models import ConnectionState, SessionSummary, validate_title


def test_local_flag_overrides_the_embedded_production_url() -> None:
    config = parse_runtime_config(["--local"])

    assert config.server_url == "http://127.0.0.1:8000"
    assert config.environment == "local"


def test_local_flag_takes_the_port_the_platform_is_serving_on() -> None:
    """The one thing that genuinely varies between machines running a backend."""
    config = parse_runtime_config(["--local", "9000"])

    assert config.server_url == "http://127.0.0.1:9000"
    assert config.environment == "local"


def test_local_flag_rejects_a_port_that_is_not_one() -> None:
    for port in ("0", "65536", "-1", "eight-thousand"):
        with pytest.raises(SystemExit):
            parse_runtime_config(["--local", port])


def test_dev_flag_uses_the_compiled_development_url() -> None:
    config = parse_runtime_config(["--dev"])

    assert config.server_url == DEVELOPMENT_SERVER_URL
    assert config.environment == "development"


def test_default_config_uses_the_compiled_production_url() -> None:
    config = parse_runtime_config([])

    assert PRODUCTION_SERVER_URL == "https://broccoli.bosch-digital-factory.com"
    assert config.server_url == "https://broccoli.bosch-digital-factory.com"
    assert config.environment == "production"


def test_every_environment_carries_the_same_compiled_websocket_path() -> None:
    for argv in ([], ["--local"], ["--dev"]):
        assert parse_runtime_config(argv).websocket_path == "/ws/listening/"


def test_no_environment_variable_can_redirect_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both routes now ship in the binary, so neither is an injection point."""
    monkeypatch.setenv("BROCCOLI_DESKTOP_WEBSOCKET_PATH", "/somewhere-else/")
    monkeypatch.setenv("BROCCOLI_DESKTOP_DEV_URL", "https://elsewhere.example")

    assert parse_runtime_config([]).websocket_path == WEBSOCKET_PATH
    assert parse_runtime_config(["--dev"]).server_url == DEVELOPMENT_SERVER_URL


def test_config_rejects_conflicting_local_and_dev_flags() -> None:
    with pytest.raises(SystemExit):
        parse_runtime_config(["--local", "--dev"])


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


def test_main_prefers_the_runtime_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from broccoli_desktop import __main__

    received: list[object] = []
    runtime = types.ModuleType("broccoli_desktop.runtime")
    runtime.start_runtime = received.append
    monkeypatch.setitem(sys.modules, "broccoli_desktop.runtime", runtime)

    __main__.main([])

    assert received[0].server_url == "https://broccoli.bosch-digital-factory.com"


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
