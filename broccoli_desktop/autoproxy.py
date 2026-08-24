"""Resolve a proxy automatic-configuration script through Windows itself.

A PAC script is JavaScript: ``FindProxyForURL(url, host)`` decides, per URL,
which proxy to use. Evaluating that in Python would mean shipping a JavaScript
engine for one function. Windows already has one -- WinHTTP downloads the
script, runs it, caches it, and hands back a concrete proxy -- so this module
asks WinHTTP and translates its answer into the ``host``/``port`` pair the rest
of the application already knows how to route through.

The application talks to a single backend, so this runs once per resolution
rather than per request. It is blocking network work all the same: callers must
keep it off the event loop (see ``Services.ensure_proxy_resolved``).
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_void_p, wintypes
from dataclasses import dataclass

#: The default a PAC script implies when it names a proxy with no port. Every
#: client shares this convention; PAC has no way to spell "the usual port".
DEFAULT_PROXY_PORT = 80

_ACCESS_TYPE_NO_PROXY = 1
_AUTOPROXY_CONFIG_URL = 0x00000002
#: Let WinHTTP answer a proxy's authentication challenge with the logged-on
#: user's credentials while *fetching the script itself*. This is about reaching
#: the script, not about the traffic that follows: the proxy the script names is
#: authenticated separately, with the username and password from the settings
#: screen.
_AUTOPROXY_AUTO_LOGIN = True
#: WinHttpGetProxyForUrl allocates the strings it returns and hands ownership to
#: the caller, who releases them with GlobalFree.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GlobalFree.argtypes = (c_void_p,)
_kernel32.GlobalFree.restype = c_void_p


class AutoProxyError(RuntimeError):
    """The configuration script could not be fetched, or did not answer.

    Distinct from "the script said DIRECT", which is a successful resolution
    with no proxy in it, and from "the proxy refused us", which happens later
    and is what the connection test reports.
    """


@dataclass(frozen=True)
class ResolvedProxy:
    """One proxy a configuration script chose for one target URL."""

    host: str
    port: int


class _AutoProxyOptions(Structure):
    _fields_ = (
        ("dwFlags", wintypes.DWORD),
        ("dwAutoDetectFlags", wintypes.DWORD),
        ("lpszAutoConfigUrl", wintypes.LPCWSTR),
        ("lpvReserved", c_void_p),
        ("dwReserved", wintypes.DWORD),
        ("fAutoLoginIfChallenged", wintypes.BOOL),
    )


class _ProxyInfo(Structure):
    """WinHTTP's answer, with its two strings kept as raw pointers.

    Typing them ``LPWSTR`` would be more convenient to read, but ctypes then
    converts each one to a ``str`` on attribute access and the pointer is gone
    -- along with any way to hand the memory back. Both strings are allocated by
    WinHTTP and owned by this process, so the pointer is the part that matters.
    """

    _fields_ = (
        ("dwAccessType", wintypes.DWORD),
        ("lpszProxy", c_void_p),
        ("lpszProxyBypass", c_void_p),
    )


def parse_proxy_list(value: str | None) -> ResolvedProxy | None:
    """Read WinHTTP's answer, which is a list even when it holds one entry.

    Entries are separated by semicolons or whitespace -- WinHTTP does not
    promise which -- and may be scoped to a scheme, as in ``http=proxy:8080``.
    Only one proxy can be handed to the HTTP client, and the list arrives in the
    script author's order of preference, so the first entry is the answer.

    Returns None for an empty answer: a script that resolves to DIRECT leaves
    nothing to report, and connecting straight out is a real instruction rather
    than a failure.
    """
    if not value or not value.strip():
        return None
    first = value.replace(";", " ").split()[0]
    _, _, endpoint = first.rpartition("=")
    host, separator, port = endpoint.rpartition(":")
    if not separator:
        return ResolvedProxy(host=endpoint, port=DEFAULT_PROXY_PORT)
    try:
        return ResolvedProxy(host=host, port=int(port))
    except ValueError as error:
        # Inventing a port here would route the user's traffic to somewhere
        # their script never named.
        raise ValueError(
            f"The configuration script returned an unusable proxy: {first!r}"
        ) from error


def resolve_script_proxy(target_url: str, script_url: str) -> ResolvedProxy | None:
    """Ask Windows which proxy ``script_url`` chooses for ``target_url``.

    Returns None when the script answers DIRECT. Raises ``AutoProxyError`` when
    the script could not be reached or run at all -- the case worth telling the
    user about, because every later failure would otherwise look like a broken
    proxy rather than a broken script address.
    """
    winhttp = _load_winhttp()
    session = winhttp.WinHttpOpen("Broccoli Desktop", _ACCESS_TYPE_NO_PROXY, None, None, 0)
    if not session:
        raise AutoProxyError(
            f"Windows could not open an HTTP session (error {ctypes.get_last_error()})."
        )
    try:
        options = _AutoProxyOptions(
            dwFlags=_AUTOPROXY_CONFIG_URL,
            lpszAutoConfigUrl=script_url,
            fAutoLoginIfChallenged=_AUTOPROXY_AUTO_LOGIN,
        )
        info = _ProxyInfo()
        if not winhttp.WinHttpGetProxyForUrl(session, target_url, byref(options), byref(info)):
            raise AutoProxyError(
                f"The configuration script at {script_url} could not be read "
                f"(error {ctypes.get_last_error()})."
            )
        try:
            if info.dwAccessType == _ACCESS_TYPE_NO_PROXY:
                return None
            return parse_proxy_list(_read(info.lpszProxy))
        finally:
            _free(info)
    finally:
        winhttp.WinHttpCloseHandle(session)


def _load_winhttp() -> ctypes.WinDLL:
    winhttp = ctypes.WinDLL("winhttp", use_last_error=True)
    winhttp.WinHttpOpen.restype = c_void_p
    winhttp.WinHttpOpen.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    )
    winhttp.WinHttpGetProxyForUrl.restype = wintypes.BOOL
    winhttp.WinHttpGetProxyForUrl.argtypes = (
        c_void_p,
        wintypes.LPCWSTR,
        POINTER(_AutoProxyOptions),
        POINTER(_ProxyInfo),
    )
    winhttp.WinHttpCloseHandle.argtypes = (c_void_p,)
    return winhttp


def _read(pointer: int | None) -> str | None:
    """Copy a wide string out of WinHTTP's memory without taking ownership."""
    if not pointer:
        return None
    return ctypes.cast(pointer, wintypes.LPWSTR).value


def _free(info: _ProxyInfo) -> None:
    """Release the two strings WinHttpGetProxyForUrl handed ownership of."""
    for pointer in (info.lpszProxy, info.lpszProxyBypass):
        if pointer:
            _kernel32.GlobalFree(pointer)
