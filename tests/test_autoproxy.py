from __future__ import annotations

from pytest import raises

from broccoli_desktop.autoproxy import ResolvedProxy, parse_proxy_list


def test_a_single_entry_becomes_a_host_and_a_port() -> None:
    """The ordinary answer, and the one a real corporate script returned:
    WinHTTP resolves the script's "PROXY host:port" down to a bare host:port."""
    assert parse_proxy_list("policy-detection.bosch.com:80") == ResolvedProxy(
        host="policy-detection.bosch.com", port=80
    )


def test_the_first_entry_of_a_list_wins() -> None:
    """A script may hand back a failover list. Only one proxy can be given to
    httpx, and the list is in the script author's order of preference, so the
    first is the one to use rather than an arbitrary pick."""
    assert parse_proxy_list("first.example:8080;second.example:3128") == ResolvedProxy(
        host="first.example", port=8080
    )


def test_entries_may_be_separated_by_spaces() -> None:
    """WinHTTP is documented as returning a list separated by semicolons or
    whitespace, and does not promise which."""
    assert parse_proxy_list("first.example:8080 second.example:3128") == ResolvedProxy(
        host="first.example", port=8080
    )


def test_a_scheme_prefix_is_not_part_of_the_host() -> None:
    """Entries can be scoped to a scheme, as in "http=proxy:8080". Left in, the
    prefix would travel into the proxy URL and produce a hostname nothing
    resolves."""
    assert parse_proxy_list("http=proxy.example:8080") == ResolvedProxy(
        host="proxy.example", port=8080
    )


def test_an_entry_without_a_port_falls_back_to_the_http_proxy_default() -> None:
    """PAC scripts are allowed to name a proxy with no port, and the convention
    every client shares for that is 80."""
    assert parse_proxy_list("proxy.example") == ResolvedProxy(host="proxy.example", port=80)


def test_no_proxy_means_a_direct_connection() -> None:
    """A script that answers DIRECT leaves WinHTTP with nothing to report, and
    that is a real answer -- connect straight out -- not a failure."""
    assert parse_proxy_list(None) is None
    assert parse_proxy_list("") is None
    assert parse_proxy_list("   ") is None


def test_an_unusable_entry_is_refused_rather_than_guessed_at() -> None:
    """Better to tell the user their script returned something unusable than to
    invent a port and route their traffic somewhere they never named."""
    with raises(ValueError):
        parse_proxy_list("proxy.example:not-a-port")
