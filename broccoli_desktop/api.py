"""Loopback-only FastAPI API used by the embedded Broccoli Desktop UI."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, quote

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from broccoli_desktop.autoproxy import AutoProxyError, ResolvedProxy, resolve_script_proxy
from broccoli_desktop.capture import (
    AudioLevelMonitor,
    AudioLevelSnapshot,
    CaptureBackend,
    DeviceUnavailableError,
)
from broccoli_desktop.credentials import CredentialStorageError
from broccoli_desktop.models import (
    ConnectionState,
    DeviceDescriptor,
    SegmentPage,
    SessionPage,
    SessionSummary,
    TranscriptDelta,
    TranscriptSegment,
    UiEvent,
    validate_title,
)
from broccoli_desktop.naming import SessionTitleGenerator
from broccoli_desktop.remote import (
    ListeningRemote,
    RemoteConflictError,
    RemoteCreditError,
    RemoteDurationError,
    RemoteError,
    RemoteNotFoundError,
    RemoteProtocolError,
    RemoteRequestError,
    RemoteUnauthorizedError,
    RemoteValidationError,
)
from broccoli_desktop.session import (
    DETECT_LANGUAGES,
    SUPPORTED_LANGUAGES,
    CaptureChoices,
    CaptureLanguages,
    DesktopSessionController,
)
from broccoli_desktop.settings import (
    DeviceSettings,
    InMemoryDeviceSettings,
    InMemoryProxySettings,
    ProxyMode,
    ProxySettings,
    ProxySettingsStore,
)

LOOPBACK_HOST = "127.0.0.1"
STATIC_DIRECTORY = Path(__file__).with_name("static")

#: Long enough that a burst of calls costs one enumeration, short enough that
#: plugging in a headset shows up without a restart.
DEVICE_CACHE_TTL_SECONDS = 5.0

#: Bounds the "Testar conexão" probe so a proxy that never answers cannot hang
#: the settings screen -- generous next to REQUEST_TIMEOUT in remote.py since
#: this is a one-off manual check, not a request on the hot path.
PROXY_TEST_TIMEOUT = httpx.Timeout(8.0, connect=5.0)


def _build_proxy_url(host: str, port: int, username: str, password: str | None) -> str:
    """Build ``http://user:pass@host:port``, omitting credentials entirely when
    no username was configured -- an unauthenticated proxy is a normal setup,
    not a half-filled one.
    """
    if not username:
        return f"http://{host}:{port}"
    userinfo = quote(username, safe="")
    if password:
        userinfo = f"{userinfo}:{quote(password, safe='')}"
    return f"http://{userinfo}@{host}:{port}"


async def _default_proxy_prober(target_url: str, proxy_url: str | None) -> bool:
    """Report only whether a request reached ``target_url`` through the proxy.

    ``proxy_url`` is None when there is no proxy to go through -- a
    configuration script is entitled to answer DIRECT, and the honest test of
    that answer is a direct request rather than a claim of success. httpx
    reads None as exactly that.

    Deliberately collapses every failure -- DNS, a refused connection, a proxy
    auth challenge, a 5xx from the target -- into the same ``False``. Anything
    more specific would let this become a credential oracle for whoever can
    reach the loopback API: a caller could tell "wrong password" apart from
    "host unreachable" by the shape of the answer alone.

    A proxy auth rejection does not always arrive as an exception: for an
    HTTPS target httpx tunnels through a CONNECT request, and a rejected
    CONNECT does raise (httpx.ProxyError, a subclass of httpx.HTTPError,
    already handled below) -- but for a plain HTTP target the proxy simply
    forwards the request and relays its own 407 back as an ordinary response.
    Checking the status here does not reopen the oracle: it maps that 407 to
    the same False an unreachable host already produces, adding no new
    distinguishing signal, only correcting a false positive on HTTP targets.
    """
    if not target_url:
        return False
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=PROXY_TEST_TIMEOUT) as client:
            response = await client.get(target_url)
    except httpx.HTTPError:
        return False
    return response.status_code != 407


class CredentialStoreProtocol(Protocol):
    """The credential operations the local API needs."""

    def load_token(self) -> str | None: ...

    def save_token(self, token: str) -> None: ...

    def delete_token(self) -> None: ...

    def load_proxy_password(self) -> str | None: ...

    def save_proxy_password(self, password: str) -> None: ...

    def delete_proxy_password(self) -> None: ...


type RemoteFactory = Callable[[str], ListeningRemote]
type ControllerFactory = Callable[[ListeningRemote, CaptureBackend], DesktopSessionController]
#: (target_url, proxy_url) -> whether a request reached the target through the
#: proxy. Injected so tests never need a real network or a real proxy.
type ProxyProber = Callable[[str, str | None], Awaitable[bool]]
#: (target_url, script_url) -> the proxy the script chose, or None for a
#: direct connection. Injected for the same reason as ProxyProber: the real
#: one downloads a script over the real network.
type ScriptResolver = Callable[[str, str], ResolvedProxy | None]


@dataclass
class Services:
    """Injectable local dependencies and the authenticated session lifetime."""

    credentials: CredentialStoreProtocol
    remote_factory: RemoteFactory
    capture_backend: CaptureBackend
    loopback_port: int | None = None
    #: None skips the check entirely -- production always sets a random one
    #: (see LoopbackServer); this exists so tests/visual_server.py can pin a
    #: known value instead.
    capability_token: str | None = None
    controller_factory: ControllerFactory = DesktopSessionController
    device_settings: DeviceSettings = field(default_factory=InMemoryDeviceSettings)
    proxy_settings: ProxySettingsStore = field(default_factory=InMemoryProxySettings)
    #: The backend origin the "Testar conexão" probe is aimed at -- production
    #: wires this to the same server the app talks to. Left blank by default so
    #: an injected Services never reaches the network unless a test opts in.
    backend_url: str = ""
    proxy_prober: ProxyProber = _default_proxy_prober
    script_resolver: ScriptResolver = resolve_script_proxy
    session_titles: SessionTitleGenerator = field(default_factory=SessionTitleGenerator)
    #: Moves the native window between its two fixed sizes. None wherever there
    #: is no native window to move -- browser_only.py and the API tests.
    window_mode: Callable[[bool], None] | None = None
    controller: DesktopSessionController | None = field(default=None, init=False)
    _remote: ListeningRemote | None = field(default=None, init=False, repr=False)
    _token: str | None = field(default=None, init=False, repr=False)
    #: (script address, what it resolved to). Resolving is a network round
    #: trip and every rebuilt transport needs the answer, so it is kept until
    #: the address changes -- see ensure_proxy_resolved.
    _resolved_script: tuple[str, ResolvedProxy | None] | None = field(
        default=None, init=False, repr=False
    )
    _device_cache: tuple[float, list[DeviceDescriptor]] | None = field(
        default=None, init=False, repr=False
    )
    audio_levels: AudioLevelMonitor = field(init=False)

    def __post_init__(self) -> None:
        self.audio_levels = AudioLevelMonitor(self.capture_backend)

    async def list_devices(self) -> list[DeviceDescriptor]:
        """Enumerate capture devices off the loop, at most once per TTL.

        Windows device enumeration routinely takes hundreds of milliseconds, and
        without this it ran on every /api/devices call, every bootstrap and
        every session start -- each one freezing the event socket and the meter
        stream with it.
        """
        cached = self._device_cache
        now = time.monotonic()
        if cached is not None and now - cached[0] < DEVICE_CACHE_TTL_SECONDS:
            return cached[1]
        devices = await asyncio.to_thread(self.capture_backend.list_devices)
        self._device_cache = (now, devices)
        return devices

    async def authenticated(self) -> tuple[DesktopSessionController, ListeningRemote] | None:
        """Return the controller and remote for the persisted token, if any.

        Reads the Windows Credential Manager off the loop -- this runs on every
        request, including each /api/events connect, and a vault read is not
        instant.
        """
        token = await asyncio.to_thread(self.credentials.load_token)
        if token is None:
            return None
        # Both branches below build a remote through remote_factory, which
        # reads proxy_url() synchronously. The script behind that answer has
        # to be downloaded first, and not on this thread.
        await self.ensure_proxy_resolved()
        if self._token != token or self.controller is None:
            remote = self.remote_factory(token)
            self._remote = remote
            self.controller = await self._create_controller(remote)
            self._token = token
        elif self._remote is None:
            # invalidate_remote() dropped the transport without disturbing the
            # controller. Rebuild only the transport and hand it over, rather
            # than falling into the branch above: replacing the controller
            # would leave every open /api/events socket subscribed to the
            # EventHub of a controller nothing publishes to any more, and the
            # window's feed would go quiet with no visible cause.
            remote = self.remote_factory(token)
            self._remote = remote
            try:
                self.controller.set_remote(remote)
            except RuntimeError:
                # A run started between the settings save and this rebuild.
                # save_settings refuses while a capture is active, but nothing
                # holds that state still between the two, and set_remote
                # refuses to swap the transport a live stream was opened
                # through. Keeping the live run on its own transport is the
                # right answer -- the alternative is the two disagreeing about
                # where the audio goes -- so this run finishes on the old one
                # and the next idle rebuild brings the two back together. The
                # superseded remote stays usable: aclose only returns its
                # pooled connections, and its client is rebuilt on demand.
                pass
        return self.controller, self._remote

    def invalidate_remote(self) -> None:
        """Forget the cached remote so the next ``authenticated()`` rebuilds it.

        ``remote_factory`` reads ``proxy_url()`` at the moment it runs, and
        ``authenticated()`` only reran it when the *token* changed. So a proxy
        saved after login stayed inert until sign-out or a restart -- while
        "Testar conexão" reported success, actively telling the user the thing
        it had just failed to apply was working.

        Callers refuse to run while a capture is active (see save_settings), so
        the controller this leaves in place is always an idle one, whose next
        run will open its stream through the rebuilt transport.
        """
        previous = self._remote
        self._remote = None
        _release_remote(previous)

    async def set_authenticated(self, token: str, remote: ListeningRemote) -> None:
        """Install the already verified remote after credential persistence succeeds."""
        previous = self._remote
        self._remote = remote
        self.controller = await self._create_controller(remote)
        self._token = token
        _release_remote(previous)

    def clear_authenticated(self) -> None:
        """Forget in-memory authenticated dependencies after local credential deletion."""
        previous = self._remote
        self._remote = None
        self.controller = None
        self._token = None
        _release_remote(previous)

    def save_selected_devices(self, choices: CaptureChoices) -> None:
        """Persist only opaque IDs after a capture period was successfully opened."""
        self.device_settings.save(choices)

    def start_audio_level_monitor(self, choices: CaptureChoices) -> None:
        """Open a temporary local meter instead of sending audio to the remote service."""
        self.audio_levels.start(choices.microphone_id, choices.system_device_id)

    def stop_audio_level_monitor(self) -> None:
        self.audio_levels.stop()

    def stop_audio_level_test(self) -> None:
        """Release a settings-check meter without disturbing a running capture.

        ``AudioLevelMonitor.stop`` also clears the capture flag, so a stream that
        closes after the capture took over the meter must leave it alone: doing
        otherwise blanks the footer histogram of a live meeting.
        """
        if self.audio_levels.capture_active:
            return
        self.audio_levels.stop()

    def clear_selected_devices(self) -> None:
        """Clear the durable selection when the user restores audio defaults."""
        self.device_settings.clear()
        if self.controller is not None:
            self.controller.clear_selected_devices()

    async def ensure_proxy_resolved(self) -> None:
        """Read the configuration script, off the loop, before a remote is built.

        proxy_url() has to stay synchronous -- remote_factory calls it inside a
        closure -- and a script is a network download rather than the cheap
        vault read that method was designed around. Doing it inline would hold
        the event loop, and with it the transcript socket, the event feed and
        the level meter, for as long as the script server takes to answer.

        So the download happens here, on a worker thread, and proxy_url() only
        reads what this left behind. Callers run it immediately before building
        a remote; a cached answer for the same address costs nothing.
        """
        settings = self.proxy_settings.load()
        if not settings.enabled or settings.mode is not ProxyMode.SCRIPT:
            return
        script_url = settings.script_url.strip()
        if not script_url:
            return
        if self._resolved_script is not None and self._resolved_script[0] == script_url:
            return
        try:
            resolved = await asyncio.to_thread(self.script_resolver, self.backend_url, script_url)
        except (AutoProxyError, ValueError):
            # A mistyped address or a script server that is down must not raise
            # out of whichever request happened to rebuild the transport. The
            # connection made without a proxy fails on its own on a network that
            # needs one, and "Testar conexão" is where the user is told why.
            logging.getLogger(__name__).warning(
                "The proxy configuration script could not be read.", exc_info=True
            )
            resolved = None
        self._resolved_script = (script_url, resolved)

    def _script_proxy(self) -> ResolvedProxy | None:
        """What ensure_proxy_resolved last got out of the configuration script."""
        if self._resolved_script is None:
            logging.getLogger(__name__).warning(
                "A proxy configuration script is set but was never resolved."
            )
            return None
        return self._resolved_script[1]

    def proxy_url(self) -> str | None:
        """Build the proxy URL a remote should route through, or None when unset.

        Reads the vault password at call time rather than caching it, so a
        password rotated or cleared mid-run is honoured by the very next remote
        this builds -- this only runs at login and at token change, never on
        the request hot path, so a vault read here is cheap enough to not
        thread through asyncio.to_thread the way credentials.load_token() does.

        In script mode the host and port come from what the script chose rather
        than from the settings file; the username and password still come from
        the settings screen, which is the only place a script cannot speak for.
        """
        settings = self.proxy_settings.load()
        if not settings.enabled:
            return None
        if settings.mode is ProxyMode.SCRIPT:
            resolved = self._script_proxy()
            if resolved is None:
                # Either the script answered DIRECT, which is an instruction to
                # connect straight out, or it could not be read at all -- which
                # ensure_proxy_resolved has already reported.
                return None
            host, port = resolved.host, resolved.port
        else:
            host, port = settings.host, settings.port
        if not host or not port:
            return None
        password = self.credentials.load_proxy_password()
        if settings.username and password is None:
            # A configured username with no vault password behind it is an
            # inconsistent state -- a local file write that landed while the
            # matching vault write did not (see save_proxy_settings's
            # ordering), or the vault entry removed out from under this
            # process -- never "this proxy needs no password". Refusing here
            # fails closed instead of silently connecting unauthenticated.
            return None
        return _build_proxy_url(host, port, settings.username, password)

    def save_proxy_settings(self, settings: ProxySettings, *, password: str | None) -> None:
        """Save a submitted password to the vault before persisting the
        connection settings that mark the proxy enabled.

        In that order deliberately: if the vault write fails partway through
        (CredentialStorageError, propagated to the client as a 503), nothing
        has been marked enabled yet, so this never leaves an "enabled" proxy
        on disk with no password behind it, silently falling back to an
        unauthenticated connection.

        Only touches the vault when a new password was actually submitted --
        the saved password never comes back to the settings screen, so the
        form resubmits with an empty password field unless the user typed a
        new one, and treating that as "leave it alone" is what lets
        host/port/username be edited without forcing a retype every time.
        """
        if password:
            self.credentials.save_proxy_password(password)
        self.proxy_settings.save(settings)
        # A different script address is a different answer, and the cached
        # one would otherwise outlive the setting that produced it.
        self._resolved_script = None
        self.invalidate_remote()

    def clear_proxy_settings(self) -> None:
        """Delete the vault password before marking the proxy disabled locally.

        Disable the proxy and delete its stored password -- not merely stop
        using it. A password left behind in the vault after the user turned
        the proxy off is still a credential this product promised would only
        live there while needed. This order is what keeps a failed vault
        delete (CredentialStorageError, propagated to the client as a 503)
        from leaving the proxy showing "disabled" locally while the password
        it was supposed to take with it is still sitting in the vault.
        """
        self.credentials.delete_proxy_password()
        self.proxy_settings.clear()
        self._resolved_script = None
        self.invalidate_remote()

    async def _create_controller(self, remote: ListeningRemote) -> DesktopSessionController:
        controller = self.controller_factory(remote, self.capture_backend)
        controller.set_device_lookup(self.list_devices)
        controller.set_authentication_failure_handler(self._on_background_authentication_failure)
        controller.set_audio_level_callbacks(
            self.audio_levels.record,
            self.audio_levels.set_capture_active,
        )
        selection = self.device_settings.load()
        if selection is not None:
            if await _choices_are_available(self, selection):
                controller.restore_selected_devices(selection)
            else:
                self.device_settings.clear()
        return controller

    def _on_background_authentication_failure(self) -> None:
        """Forget the local credential without handing its value to the controller or UI."""
        try:
            self.credentials.delete_token()
        except CredentialStorageError:
            pass
        finally:
            self.clear_authenticated()


class ApiError(Exception):
    """A client-safe API failure that never embeds remote or credential data."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail


class LoginRequest(BaseModel):
    token: str


class WindowModeRequest(BaseModel):
    mode: Literal["compact", "maximized"]


class SessionRequest(BaseModel):
    microphone_id: str
    system_device_id: str


class CaptureRequest(SessionRequest):
    """A request that opens a capture period, device selection plus language.

    The languages default to the empty string, which is what asks the
    service to detect them -- the behaviour every capture had before this
    choice existed.
    """

    microphone_language: str = ""
    system_language: str = ""


class StartSessionRequest(CaptureRequest):
    title: str = ""


class MuteRequest(BaseModel):
    """The complete mute state of both channels, not a single toggle.

    Sending both every time keeps the window and the capture from drifting
    apart: a request that lost its way cannot leave one channel silently
    muted while the interface shows it live.
    """

    microphone_muted: bool
    system_muted: bool


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    is_pinned: bool | None = None


class ProxyPayload(BaseModel):
    """Deliberately mirrors ProxySettings plus one write-only field: a password
    that is only ever read here, on the way into the vault, and never appears
    on any model this API returns.
    """

    enabled: bool
    mode: ProxyMode = ProxyMode.MANUAL
    host: str = ""
    port: int = 0
    #: Only read in script mode, where it replaces host and port entirely.
    script_url: str = ""
    username: str = ""
    #: None means "leave whatever is already stored" -- the saved password
    #: never round-trips to the settings screen, so there is nothing to resend
    #: unless the user actually typed a new one.
    password: str | None = None


class SettingsRequest(BaseModel):
    proxy: ProxyPayload


class ProxyTestRequest(BaseModel):
    """The candidate values currently in the settings form, tested as typed --
    independent of whatever is already saved or enabled."""

    mode: ProxyMode = ProxyMode.MANUAL
    host: str = ""
    port: int = 0
    script_url: str = ""
    username: str = ""
    password: str | None = None


class LoopbackHostMiddleware:
    """Reject Host headers that could expose this UI outside its local origin."""

    def __init__(self, app: Any, *, port: int | None, capability_token: str | None = None) -> None:
        self.app = app
        self._allowed_hosts = {"localhost", LOOPBACK_HOST}
        self._allowed_origins: set[str] = set()
        self._capability_token = capability_token
        if port is not None:
            self._allowed_hosts.update({f"localhost:{port}", f"{LOOPBACK_HOST}:{port}"})
            self._allowed_origins.update(
                {f"http://localhost:{port}", f"http://{LOOPBACK_HOST}:{port}"}
            )

    @staticmethod
    def _header(scope: dict[str, Any], name: bytes) -> str:
        return next(
            (
                value.decode("latin-1")
                for key, value in scope.get("headers", [])
                if key.lower() == name
            ),
            "",
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        host = self._header(scope, b"host")
        if host.lower() not in self._allowed_hosts:
            await self._reject(scope, send, status=400, detail="Local host required.")
            return
        # A browser always sends Origin on a WebSocket handshake and on any
        # cross-origin fetch, and WebSockets are not covered by the same-origin
        # policy -- so this, not the Host header, is what separates the app's own
        # window from a page the user happens to be visiting. A request with no
        # Origin is a local non-browser caller and is left alone.
        origin = self._header(scope, b"origin")
        if origin and origin.lower() not in self._allowed_origins:
            await self._reject(scope, send, status=403, detail="Local origin required.")
            return
        # Origin stops a web page the user happens to be visiting. It does not
        # stop another process on the machine, which sends no Origin at all --
        # this per-launch key is what closes that gap. / and /static/* are the
        # shell and its assets: no data, and the page cannot present a key
        # before it has loaded the script that reads one. Neither WebSocket nor
        # EventSource can set a request header, so a missing header falls back
        # to the `k` query parameter for every /api/* request, not only sockets.
        if self._capability_token and scope.get("path", "").startswith("/api/"):
            presented = self._header(scope, b"x-broccoli-key")
            if not presented:
                presented = parse_qs(scope.get("query_string", b"").decode("latin-1")).get(
                    "k", [""]
                )[0]
            # Compared as bytes. secrets.compare_digest raises TypeError on a
            # str with any character above U+007F, and _header decodes headers
            # latin-1 -- so a non-ASCII X-Broccoli-Key produced an unhandled
            # 500 where it should have produced the same 403 as any other wrong
            # key. Encoding both sides keeps the comparison constant-time and
            # makes a garbage key a rejection rather than a stack trace.
            if not secrets.compare_digest(
                presented.encode("utf-8"), self._capability_token.encode("utf-8")
            ):
                await self._reject(scope, send, status=403, detail="Local key required.")
                return
        await self.app(scope, receive, send)

    async def _reject(self, scope: dict[str, Any], send: Any, *, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"detail": detail}).encode("utf-8"),
            }
        )


# Matches the `k` query parameter uvicorn prints verbatim in two independent
# places: the HTTP access log (`uvicorn.access`, gated by `access_log` -- but
# EventSource and the initial window navigation are plain GETs that go
# through it) and the WebSocket handshake log, which uses a *different*
# logger (`uvicorn.error`) that `access_log=False` does not touch at all.
_CAPABILITY_KEY_QUERY_PATTERN = re.compile(r'([?&]k=)[^&\s"]*')


class _RedactCapabilityKeyFilter(logging.Filter):
    """Strip the loopback capability token out of uvicorn's own log lines.

    Neither WebSocket nor EventSource can carry the token as a header, so it
    travels in the URL's `k` query parameter instead -- and uvicorn logs that
    raw URL verbatim on every request/handshake. Redacting in place (rather
    than disabling the loggers outright) keeps the rest of the line -- method,
    path, status code -- intact for anyone troubleshooting from these logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _CAPABILITY_KEY_QUERY_PATTERN.sub(r"\1REDACTED", value)
                if isinstance(value, str)
                else value
                for value in record.args
            )
        elif isinstance(record.msg, str):
            record.msg = _CAPABILITY_KEY_QUERY_PATTERN.sub(r"\1REDACTED", record.msg)
        return True


def _install_capability_key_log_redaction() -> None:
    """Attach the redaction filter to both loggers that print the raw URL.

    Safe to call more than once: a logger already carrying the filter is left
    alone instead of gaining a second one.
    """
    for name in ("uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(name)
        if not any(isinstance(existing, _RedactCapabilityKeyFilter) for existing in target.filters):
            target.addFilter(_RedactCapabilityKeyFilter())


def create_uvicorn_config(app: FastAPI, *, port: int) -> uvicorn.Config:
    """Return a Uvicorn configuration that never binds an external interface.

    The graceful-shutdown timeout is explicit because the default waits forever
    and the SSE meter stream never ends on its own.

    Constructing ``uvicorn.Config`` runs its one-time ``configure_logging()``,
    which is why the redaction filter is installed after it: anything
    attached before would be wiped out by that call, and nothing later in
    ``UvicornLoopbackServer`` or ``tests/visual_server.py`` reconfigures
    logging again.
    """
    config = uvicorn.Config(app, host=LOOPBACK_HOST, port=port, timeout_graceful_shutdown=3)
    _install_capability_key_log_redaction()
    return config


def create_app(services: Services) -> FastAPI:
    """Create the loopback JSON API with only injectable local dependencies."""
    app = FastAPI()
    app.add_middleware(
        LoopbackHostMiddleware,
        port=services.loopback_port,
        capability_token=services.capability_token,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Invalid request."})

    @app.exception_handler(CredentialStorageError)
    async def credential_storage_error_handler(
        _request: Request, _error: CredentialStorageError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": "Credential storage is unavailable."}
        )

    # Every remote call answers the same five failures the same way. Mapping them
    # once here keeps each endpoint about its own logic; an endpoint that needs a
    # more actionable message still catches the exception and raises its own
    # ApiError, which this file's first handler turns into the response.
    @app.exception_handler(RemoteUnauthorizedError)
    async def remote_unauthorized_handler(
        _request: Request, _error: RemoteUnauthorizedError
    ) -> JSONResponse:
        _delete_invalid_credential(services)
        return JSONResponse(status_code=401, content={"detail": "Authentication is required."})

    @app.exception_handler(RemoteNotFoundError)
    async def remote_not_found_handler(
        _request: Request, _error: RemoteNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"detail": "The requested session was not found."}
        )

    @app.exception_handler(RemoteConflictError)
    async def remote_conflict_handler(
        _request: Request, _error: RemoteConflictError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": "The current session cannot be changed."}
        )

    @app.exception_handler(RemoteValidationError)
    async def remote_validation_handler(
        _request: Request, _error: RemoteValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "The session changes are invalid."})

    @app.exception_handler(RemoteCreditError)
    async def remote_credit_handler(_request: Request, _error: RemoteCreditError) -> JSONResponse:
        # 402: the request was understood and the credential is fine -- there is
        # simply nothing left to spend. Without this handler the refusal fell
        # through to a 500, and before the platform started sending a readable
        # close code it reached the window as "authentication failed", which
        # sent people off to re-enter a token that was never the problem.
        return JSONResponse(
            status_code=402,
            content={"detail": "The account has no Listening credit available."},
        )

    @app.exception_handler(RemoteDurationError)
    async def remote_duration_handler(
        _request: Request, _error: RemoteDurationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "The session reached its maximum duration."},
        )

    @app.exception_handler(RemoteRequestError)
    async def remote_request_handler(_request: Request, _error: RemoteRequestError) -> JSONResponse:
        return _remote_unavailable()

    @app.exception_handler(RemoteProtocolError)
    async def remote_protocol_handler(
        _request: Request, _error: RemoteProtocolError
    ) -> JSONResponse:
        return _remote_unavailable()

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        """Report loopback readiness without reading credentials or remote state."""
        return {"status": "ok"}

    @app.post("/api/login", status_code=204)
    async def login(request: LoginRequest) -> Response:
        token = request.token.strip()
        if not token:
            raise ApiError(422, "A credential is required.")
        if services.controller is not None and _capture_is_active(services.controller):
            raise ApiError(409, "A capture is active.")
        await services.ensure_proxy_resolved()
        remote = services.remote_factory(token)
        try:
            await remote.verify_token()
        except RemoteUnauthorizedError:
            services.credentials.delete_token()
            services.clear_authenticated()
            raise ApiError(401, "Authentication is required.") from None
        services.credentials.save_token(token)
        await services.set_authenticated(token, remote)
        return Response(status_code=204)

    @app.delete("/api/login", status_code=204)
    async def logout() -> Response:
        services.stop_audio_level_monitor()
        controller = services.controller
        if controller is not None:
            # Signing out must not depend on the remote being reachable. A failure
            # here used to surface as a 500 with the token still in the credential
            # store, locking the user into the session they asked to leave.
            try:
                await controller.stop()
            except (RemoteUnauthorizedError, RemoteRequestError, RemoteProtocolError):
                pass
        services.credentials.delete_token()
        services.clear_authenticated()
        return Response(status_code=204)

    @app.get("/api/settings")
    async def get_settings() -> dict[str, object]:
        """Reachable without authentication: a corporate network may need this
        proxy configured before the login request itself can even reach the
        backend."""
        return {"proxy": _proxy_payload(services.proxy_settings.load())}

    @app.post("/api/settings", status_code=204)
    async def save_settings(request: SettingsRequest) -> Response:
        # Saving drops the cached remote so the next request rebuilds it
        # through the new route. A live run holds a stream opened through the
        # old one, and swapping the transport under it would leave the two
        # disagreeing about where the audio goes -- so a running capture is
        # refused here rather than silently ignored, the same way an audio
        # device change is.
        if services.controller is not None and _capture_is_active(services.controller):
            raise ApiError(409, "Stop the active capture before changing the proxy.")
        proxy = request.proxy
        if not proxy.enabled:
            services.clear_proxy_settings()
            return Response(status_code=204)
        username = proxy.username.strip()
        if proxy.mode is ProxyMode.SCRIPT:
            script_url = proxy.script_url.strip()
            if not script_url:
                # Same reason the manual mode refuses a missing host and port:
                # an enabled proxy with nothing to route through is a setting
                # that silently does nothing.
                raise ApiError(422, "A proxy configuration script address is required.")
            settings = ProxySettings(
                enabled=True,
                mode=ProxyMode.SCRIPT,
                script_url=script_url,
                username=username,
            )
        else:
            host = proxy.host.strip()
            if not host or not proxy.port:
                raise ApiError(422, "Proxy host and port are required.")
            settings = ProxySettings(
                enabled=True,
                mode=ProxyMode.MANUAL,
                host=host,
                port=proxy.port,
                username=username,
            )
        services.save_proxy_settings(settings, password=proxy.password)
        return Response(status_code=204)

    @app.post("/api/settings/test-proxy")
    async def test_proxy(request: ProxyTestRequest) -> dict[str, bool]:
        """Try one request through the submitted (not necessarily saved) proxy
        values and report only whether it got through -- see _default_proxy_prober
        for why the answer never says more than that.

        ``script_error`` is the one distinction worth drawing, because the two
        failures need different fixes: correct the script address, or correct
        the credentials. It is no credential oracle either -- it reports on a
        URL the caller has just typed, not on what the proxy thought of a
        password.
        """
        username = request.username.strip()
        if request.mode is ProxyMode.SCRIPT:
            script_url = request.script_url.strip()
            if not script_url:
                raise ApiError(422, "A proxy configuration script address is required.")
            try:
                resolved = await asyncio.to_thread(
                    services.script_resolver, services.backend_url, script_url
                )
            except (AutoProxyError, ValueError):
                return {"ok": False, "script_error": True}
            # A script is entitled to answer DIRECT, and the honest test of that
            # is a direct request rather than reporting success on the strength
            # of the script having been readable.
            proxy_url = (
                None
                if resolved is None
                else _build_proxy_url(resolved.host, resolved.port, username, request.password)
            )
        else:
            host = request.host.strip()
            if not host or not request.port:
                raise ApiError(422, "Proxy host and port are required.")
            proxy_url = _build_proxy_url(host, request.port, username, request.password)
        ok = await services.proxy_prober(services.backend_url, proxy_url)
        return {"ok": ok, "script_error": False}

    @app.post("/api/window", status_code=204)
    def set_window_mode(request: WindowModeRequest) -> Response:
        """Follow the screen the UI is showing; the window has no other size."""
        if services.window_mode is not None:
            services.window_mode(request.mode == "maximized")
        return Response(status_code=204)

    @app.get("/api/session-name")
    async def session_name() -> dict[str, str]:
        """Suggest a readable title for a session the user is about to start.

        The draft is named before the row exists so the title the user sees is
        the title that gets persisted, and so a meeting nobody renames is still
        findable in the history.
        """
        await _require_authenticated(services)
        return {"title": services.session_titles()}

    @app.get("/api/devices")
    async def devices() -> dict[str, object]:
        return {"devices": await _device_payloads(services)}

    @app.put("/api/devices/selection", status_code=204)
    async def save_device_selection(request: SessionRequest) -> Response:
        choices = await _validated_choices(services, request)
        controller, _remote = await _require_authenticated(services)
        if _capture_is_active(controller):
            raise ApiError(409, "Stop the active capture before changing audio devices.")
        services.save_selected_devices(choices)
        controller.restore_selected_devices(choices)
        return Response(status_code=204)

    @app.delete("/api/devices/selection", status_code=204)
    async def clear_device_selection() -> Response:
        controller, _remote = await _require_authenticated(services)
        if _capture_is_active(controller):
            raise ApiError(409, "Stop the active capture before changing audio devices.")
        services.clear_selected_devices()
        return Response(status_code=204)

    @app.get("/api/audio-levels/stream")
    async def audio_levels_stream(
        microphone_id: str = "", system_device_id: str = ""
    ) -> StreamingResponse:
        """Stream coalesced microphone and system levels to the capture footer.

        Device IDs turn the connection into the settings device check: the meter
        they open belongs to this stream and is released when it closes, so a
        window that disappears cannot leave a microphone running. Without them
        the stream is a passive observer of whatever the capture is feeding.
        """
        controller, _remote = await _require_authenticated(services)
        if not microphone_id and not system_device_id:
            return _audio_level_response(services, test_choices=None)

        choices = await _validated_choices(
            services,
            SessionRequest(microphone_id=microphone_id, system_device_id=system_device_id),
        )
        if _capture_is_active(controller):
            raise ApiError(409, "Stop the active capture before testing audio devices.")
        return _audio_level_response(services, test_choices=choices)

    @app.get("/api/sessions")
    async def list_sessions(request: Request) -> dict[str, object]:
        _controller, remote = await _require_authenticated(services)
        cursor = request.query_params.get("cursor") or None
        query = (request.query_params.get("q") or "").strip()
        return _session_page_payload(await remote.list_sessions(cursor, query))

    @app.get("/api/sessions/{uuid_code}/segments")
    async def list_segments(request: Request, uuid_code: str) -> dict[str, object]:
        _controller, remote = await _require_authenticated(services)
        cursor = request.query_params.get("cursor") or None
        return _segment_page_payload(await remote.list_segments(uuid_code, cursor))

    @app.patch("/api/sessions/{uuid_code}")
    async def update_session(uuid_code: str, request: SessionUpdateRequest) -> dict[str, object]:
        controller, remote = await _require_authenticated(services)
        changes = request.model_dump(exclude_unset=True)
        if not changes:
            raise ApiError(422, "Choose a session property to update.")

        title: str | None = None
        is_pinned: bool | None = None
        if "title" in changes:
            raw_title = changes["title"]
            if raw_title is None:
                raise ApiError(422, "The session title is invalid.")
            title = _validate_title(raw_title)
        if "is_pinned" in changes:
            raw_is_pinned = changes["is_pinned"]
            if raw_is_pinned is None:
                raise ApiError(422, "The pin state is invalid.")
            is_pinned = raw_is_pinned

        session = await remote.update_session(uuid_code, title=title, is_pinned=is_pinned)
        controller.apply_session_metadata(session)
        return _session_payload(session)

    @app.delete("/api/sessions/{uuid_code}", status_code=204)
    async def delete_session(uuid_code: str) -> Response:
        _controller, remote = await _require_authenticated(services)
        try:
            await remote.delete_session(uuid_code)
        except RemoteConflictError:
            raise ApiError(409, "Stop the live capture before deleting this session.") from None
        return Response(status_code=204)

    @app.post("/api/sessions", status_code=201)
    async def start_session(request: StartSessionRequest) -> dict[str, object]:
        # Named here when the client could not name it -- a failed suggestion
        # request must not be what leaves a meeting with no title at all.
        title = _validate_title(request.title) or services.session_titles()
        choices = await _validated_choices(services, request)
        languages = _validated_languages(request)
        controller, _remote = await _require_authenticated(services)
        services.stop_audio_level_monitor()
        try:
            session = await controller.start_new(choices, title, languages=languages)
        except RuntimeError:
            raise ApiError(409, "The current session cannot be changed.") from None
        except DeviceUnavailableError:
            raise ApiError(422, "The selected capture device is unavailable.") from None
        services.save_selected_devices(choices)
        return _session_payload(session)

    @app.post("/api/sessions/{uuid_code}/resume", status_code=201)
    async def resume_session(uuid_code: str, request: CaptureRequest) -> dict[str, object]:
        choices = await _validated_choices(services, request)
        languages = _validated_languages(request)
        controller, remote = await _require_authenticated(services)
        services.stop_audio_level_monitor()
        try:
            existing = await remote.get_session(uuid_code)
            session = await controller.resume(
                uuid_code, choices, title=existing.title, languages=languages
            )
        except RuntimeError:
            raise ApiError(409, "The current session cannot be changed.") from None
        except DeviceUnavailableError:
            raise ApiError(422, "The selected capture device is unavailable.") from None
        services.save_selected_devices(choices)
        return _session_payload(session)

    @app.post("/api/sessions/stop", status_code=204)
    async def stop_session() -> Response:
        controller, _remote = await _require_authenticated(services)
        await controller.stop()
        return Response(status_code=204)

    @app.put("/api/capture/mute", status_code=204)
    async def set_capture_mute(request: MuteRequest) -> Response:
        """Withhold either channel from the remote, or send it again.

        Deliberately allowed while a capture is running -- muting the room
        for a private aside is the whole point, and it is the one control
        here that would be useless if it needed the capture stopped first.
        """
        controller, _remote = await _require_authenticated(services)
        controller.set_channel_muted("mic", request.microphone_muted)
        controller.set_channel_muted("system", request.system_muted)
        return Response(status_code=204)

    @app.websocket("/api/events")
    async def events(websocket: WebSocket) -> None:
        """Push local state to the UI, starting with the bootstrap snapshot.

        The socket is accepted even without a credential: answering with an
        unauthenticated bootstrap is what lets the login screen render from this
        one channel instead of a second HTTP route repeating the same payload.
        """
        await websocket.accept()
        authenticated = await services.authenticated()
        if authenticated is not None:
            # A *stored* credential is not a *valid* one, and nothing between
            # the vault read and here had asked the platform. So a token that
            # had been revoked since the last run still booted the window
            # straight onto the capture screen, and the user only found out
            # when their first action came back 401 -- an error toast on a
            # screen that could not work. Verifying once, here, is what turns
            # that into the login screen.
            try:
                await authenticated[1].verify_token()
            except RemoteUnauthorizedError:
                _delete_invalid_credential(services)
                authenticated = None
            except RemoteError:
                # Unreachable is not the same as rejected: a flaky network, a
                # proxy that is down, a platform mid-deploy. Keeping the
                # credential lets the window come up and fail loudly on the
                # action the user actually takes, rather than silently signing
                # them out and discarding a token that is still good.
                logging.getLogger(__name__).warning(
                    "Could not reach the platform to verify the stored credential; keeping it",
                )
        controller = authenticated[0] if authenticated is not None else None
        bootstrap = {
            "type": "bootstrap",
            "bootstrap": await _bootstrap_payload(services, controller),
        }
        if controller is None:
            # Nothing to subscribe to. The UI reconnects once a login succeeds.
            await websocket.send_json(bootstrap)
            return

        event_queue: asyncio.Queue[UiEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def subscriber(event: UiEvent) -> None:
            loop.call_soon_threadsafe(event_queue.put_nowait, event)

        controller.events.subscribe(subscriber)
        try:
            await _send_events(websocket, event_queue, bootstrap=bootstrap)
        except WebSocketDisconnect:
            pass
        finally:
            controller.events.unsubscribe(subscriber)

    @app.get("/", include_in_schema=False)
    async def root() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "index.html")

    return app


async def _send_events(
    websocket: WebSocket, event_queue: asyncio.Queue[UiEvent], *, bootstrap: dict[str, object]
) -> None:
    """Forward queued UI events until the browser goes away.

    The bootstrap is sent from in here, after the receive task exists, so the
    socket is never in the state of having spoken but not yet listening. A
    client that answers the bootstrap by closing immediately would otherwise
    race that gap and have its disconnect arrive with nobody reading it.

    The receive task also deliberately outlives one iteration. Recreating and
    cancelling it around every outgoing event risks cancelling it in the window
    between the ASGI queue handing a message over and the coroutine resuming --
    and for the one message that matters here, ``websocket.disconnect``, losing
    it means never noticing the browser left.
    """
    receive_task = asyncio.create_task(websocket.receive())
    await websocket.send_json(bootstrap)
    event_task = asyncio.create_task(event_queue.get())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {receive_task, event_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if event_task in done:
                await websocket.send_json(_event_payload(event_task.result()))
                event_task = asyncio.create_task(event_queue.get())
            if receive_task in done:
                if receive_task.result()["type"] == "websocket.disconnect":
                    return
                receive_task = asyncio.create_task(websocket.receive())
    finally:
        # Cancel and hand off, rather than awaiting the two tasks here. This
        # `finally` runs while the endpoint task is itself being cancelled --
        # a browser closing the socket, a server shutdown, a test client
        # leaving its `with` block -- and an `await` in that state raises
        # CancelledError a second time, which anyio does not absorb: it came
        # back out of WebSocketTestSession.__exit__ and turned the standard
        # suite red about one run in ten. Nothing here needs the tasks to have
        # finished before the endpoint returns; they hold a receive and a queue
        # get, and the subscriber is removed by the caller. _reap_socket_task
        # keeps a strong reference until each one really is done and retrieves
        # its result so nothing is reported as never retrieved.
        for task in (receive_task, event_task):
            task.cancel()
            _reap_socket_task(task)


#: Strong references to the cancelled event-socket tasks, held until each one
#: finishes. CPython keeps only a weak reference to a running task, so a task
#: cancelled and then dropped can be collected before the cancellation has been
#: delivered.
_SOCKET_TASKS: set[asyncio.Task[Any]] = set()


def _reap_socket_task(task: asyncio.Task[Any]) -> None:
    """Hold a cancelled socket task until it finishes, then retrieve its result."""

    def done(finished: asyncio.Task[Any]) -> None:
        _SOCKET_TASKS.discard(finished)
        if not finished.cancelled():
            # Marks any exception retrieved; a normal result is simply dropped.
            finished.exception()

    _SOCKET_TASKS.add(task)
    task.add_done_callback(done)


#: Strong references to the closers below, held until each one finishes.
#: CPython keeps only a weak reference to a running task, so a bare create_task
#: can be collected mid-close and leave the sockets it was returning open. This
#: lives at module scope because _release_remote is a module function with no
#: instance to hang it on, and because the tasks outlive the Services object
#: whose remote they are closing.
_RELEASE_TASKS: set[asyncio.Task[None]] = set()


def _release_remote(remote: ListeningRemote | None) -> None:
    """Close a superseded remote's pooled connections, best effort.

    Reached from the request path and from the controller's background failure
    callback alike, so it cannot assume a running loop and cannot await. A
    remote that outlives this call is collected with its transport; the point
    is to return the sockets promptly in the common case.
    """
    closer = getattr(remote, "aclose", None)
    if closer is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(closer())
    _RELEASE_TASKS.add(task)
    task.add_done_callback(_RELEASE_TASKS.discard)


def _remote_unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "The remote service is unavailable."})


async def _require_authenticated(
    services: Services,
) -> tuple[DesktopSessionController, ListeningRemote]:
    authenticated = await services.authenticated()
    if authenticated is None:
        raise ApiError(401, "Authentication is required.")
    return authenticated


def _delete_invalid_credential(services: Services) -> None:
    services.credentials.delete_token()
    services.clear_authenticated()


async def _validated_choices(services: Services, request: SessionRequest) -> CaptureChoices:
    by_id = {device.device_id: device for device in await services.list_devices()}
    microphone = by_id.get(request.microphone_id)
    system = by_id.get(request.system_device_id)
    if microphone is None or microphone.kind != "mic" or system is None or system.kind != "system":
        raise ApiError(422, "Select an available microphone and system device.")
    return CaptureChoices(request.microphone_id, request.system_device_id)


def _validated_languages(request: CaptureRequest) -> CaptureLanguages:
    """Accept only a language the transcription service is known to take."""
    if (
        request.microphone_language not in SUPPORTED_LANGUAGES
        or request.system_language not in SUPPORTED_LANGUAGES
    ):
        raise ApiError(422, "The selected transcription language is unsupported.")
    return CaptureLanguages(microphone=request.microphone_language, system=request.system_language)


async def _choices_are_available(services: Services, choices: CaptureChoices) -> bool:
    devices = {device.device_id: device.kind for device in await services.list_devices()}
    return (
        devices.get(choices.microphone_id) == "mic"
        and devices.get(choices.system_device_id) == "system"
    )


def _capture_is_active(controller: DesktopSessionController) -> bool:
    return controller.state in {
        ConnectionState.STARTING,
        ConnectionState.STREAMING,
        ConnectionState.RECONNECTING,
    }


def _validate_title(title: str) -> str:
    try:
        return validate_title(title)
    except ValueError:
        raise ApiError(422, "The session title is invalid.") from None


async def _bootstrap_payload(
    services: Services, controller: DesktopSessionController | None
) -> dict[str, object]:
    choices = controller.selected_devices if controller is not None else None
    languages = controller.selected_languages if controller is not None else DETECT_LANGUAGES
    muted = controller.muted_channels if controller is not None else {}
    return {
        "authenticated": controller is not None,
        "devices": await _device_payloads(services),
        # The window keeps its own copy of both -- the languages in local
        # storage, the mute state in memory -- so this is what a reload
        # lands on: the run that is actually in progress, not what the
        # window last chose.
        "languages": {"microphone": languages.microphone, "system": languages.system},
        "muted": {
            "microphone": bool(muted.get("mic")),
            "system": bool(muted.get("system")),
        },
        "selected_devices": (
            {
                "microphone_id": choices.microphone_id,
                "system_device_id": choices.system_device_id,
            }
            if choices is not None
            else None
        ),
        "state": controller.state.value if controller is not None else ConnectionState.IDLE.value,
        "session": _session_payload(controller.session)
        if controller and controller.session
        else None,
    }


async def _device_payloads(services: Services) -> list[dict[str, str]]:
    return [_device_payload(device) for device in await services.list_devices()]


def _device_payload(device: DeviceDescriptor) -> dict[str, str]:
    return {"device_id": device.device_id, "label": device.label, "kind": device.kind}


def _audio_level_response(
    services: Services, *, test_choices: CaptureChoices | None
) -> StreamingResponse:
    return StreamingResponse(
        _audio_level_events(services, test_choices),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _audio_level_events(
    services: Services, test_choices: CaptureChoices | None
) -> AsyncIterator[str]:
    """Bridge capture-thread level snapshots into one bounded async SSE queue."""
    monitor = services.audio_levels
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[AudioLevelSnapshot] = asyncio.Queue(maxsize=8)

    def enqueue(snapshot: AudioLevelSnapshot) -> None:
        def put() -> None:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass

        if not loop.is_closed():
            loop.call_soon_threadsafe(put)

    if test_choices is not None:
        # Opening inside the generator ties the device to the response body: a
        # client that never reads it never got here, so there is nothing to leak.
        try:
            services.start_audio_level_monitor(test_choices)
        except DeviceUnavailableError:
            yield _sse_event(
                "device_error", {"detail": "The selected audio device is unavailable."}
            )
            return

    monitor.subscribe(enqueue)
    try:
        yield _audio_level_sse_payload(monitor.snapshot())
        while True:
            try:
                snapshot = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                yield ": heartbeat\n\n"
            else:
                yield _audio_level_sse_payload(snapshot)
    finally:
        monitor.unsubscribe(enqueue)
        if test_choices is not None:
            # stop_audio_level_test joins two capture workers with a 1-second
            # timeout each -- off the loop, so a closing SSE stream cannot
            # freeze the transcript socket and the rest of the interface with
            # it while that join is pending.
            await asyncio.to_thread(services.stop_audio_level_test)


def _sse_event(event: str, data: dict[str, object]) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def _audio_level_sse_payload(snapshot: AudioLevelSnapshot) -> str:
    payload = json.dumps(_audio_level_payload(snapshot), separators=(",", ":"))
    return f"data: {payload}\n\n"


def _audio_level_payload(snapshot: AudioLevelSnapshot) -> dict[str, object]:
    return {
        "active": snapshot.active,
        "microphone": {"level": snapshot.microphone, "peak": snapshot.microphone_peak},
        "system": {"level": snapshot.system, "peak": snapshot.system_peak},
    }


def _proxy_payload(settings: ProxySettings) -> dict[str, object]:
    """Never includes a password field -- ProxySettings has none to include."""
    return {
        "enabled": settings.enabled,
        "mode": settings.mode.value,
        "host": settings.host,
        "port": settings.port,
        "script_url": settings.script_url,
        "username": settings.username,
    }


def _session_payload(session: SessionSummary) -> dict[str, object]:
    return {
        "uuid_code": session.uuid_code,
        "title": session.title,
        "status": session.status,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "device_label": session.device_label,
        "segment_count": session.segment_count,
        "is_live": session.is_live,
        "is_pinned": session.is_pinned,
        "pinned_at": session.pinned_at,
        "last_activity_at": session.last_activity_at,
    }


def _session_page_payload(page: SessionPage) -> dict[str, object]:
    return {
        "sessions": [_session_payload(session) for session in page.sessions],
        "next_cursor": page.next_cursor,
    }


def _segment_page_payload(page: SegmentPage) -> dict[str, object]:
    return {
        "segments": [_segment_payload(segment) for segment in page.segments],
        "next_cursor": page.next_cursor,
    }


def _segment_payload(segment: TranscriptSegment) -> dict[str, object]:
    return {
        "utterance_id": segment.utterance_id,
        "channel": segment.channel,
        "text": segment.text,
        "started_offset_ms": segment.started_offset_ms,
        "ended_offset_ms": segment.ended_offset_ms,
    }


def _delta_payload(delta: TranscriptDelta) -> dict[str, object]:
    return {
        "utterance_id": delta.utterance_id,
        "channel": delta.channel,
        "text": delta.text,
        "started_offset_ms": delta.started_offset_ms,
    }


def _event_payload(event: UiEvent) -> dict[str, object]:
    payload: dict[str, object] = {"type": event.type}
    if event.state is not None:
        payload["state"] = event.state.value
    if event.session is not None:
        payload["session"] = _session_payload(event.session)
    if event.delta is not None:
        payload["delta"] = _delta_payload(event.delta)
    if event.segment is not None:
        payload["segment"] = _segment_payload(event.segment)
    if event.message is not None:
        payload["message"] = event.message
    return payload
