"""Loopback-only FastAPI API used by the embedded Broccoli Desktop UI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    RemoteNotFoundError,
    RemoteProtocolError,
    RemoteRequestError,
    RemoteUnauthorizedError,
    RemoteValidationError,
)
from broccoli_desktop.session import CaptureChoices, DesktopSessionController
from broccoli_desktop.settings import DeviceSettings, InMemoryDeviceSettings

LOOPBACK_HOST = "127.0.0.1"
STATIC_DIRECTORY = Path(__file__).with_name("static")


class CredentialStoreProtocol(Protocol):
    """The credential operations the local API needs."""

    def load_token(self) -> str | None: ...

    def save_token(self, token: str) -> None: ...

    def delete_token(self) -> None: ...


type RemoteFactory = Callable[[str], ListeningRemote]
type ControllerFactory = Callable[[ListeningRemote, CaptureBackend], DesktopSessionController]


@dataclass
class Services:
    """Injectable local dependencies and the authenticated session lifetime."""

    credentials: CredentialStoreProtocol
    remote_factory: RemoteFactory
    capture_backend: CaptureBackend
    loopback_port: int | None = None
    controller_factory: ControllerFactory = DesktopSessionController
    device_settings: DeviceSettings = field(default_factory=InMemoryDeviceSettings)
    session_titles: SessionTitleGenerator = field(default_factory=SessionTitleGenerator)
    controller: DesktopSessionController | None = field(default=None, init=False)
    _remote: ListeningRemote | None = field(default=None, init=False, repr=False)
    _token: str | None = field(default=None, init=False, repr=False)
    audio_levels: AudioLevelMonitor = field(init=False)

    def __post_init__(self) -> None:
        self.audio_levels = AudioLevelMonitor(self.capture_backend)

    def authenticated(self) -> tuple[DesktopSessionController, ListeningRemote] | None:
        """Return the controller and remote for the persisted token, if any."""
        token = self.credentials.load_token()
        if token is None:
            return None
        if self._token != token or self._remote is None or self.controller is None:
            remote = self.remote_factory(token)
            self._remote = remote
            self.controller = self._create_controller(remote)
            self._token = token
        return self.controller, self._remote

    def set_authenticated(self, token: str, remote: ListeningRemote) -> None:
        """Install the already verified remote after credential persistence succeeds."""
        previous = self._remote
        self._remote = remote
        self.controller = self._create_controller(remote)
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

    def _create_controller(self, remote: ListeningRemote) -> DesktopSessionController:
        controller = self.controller_factory(remote, self.capture_backend)
        controller.set_authentication_failure_handler(self._on_background_authentication_failure)
        controller.set_audio_level_callbacks(
            self.audio_levels.record,
            self.audio_levels.set_capture_active,
        )
        selection = self.device_settings.load()
        if selection is not None:
            if _choices_are_available(self.capture_backend, selection):
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


class SessionRequest(BaseModel):
    microphone_id: str
    system_device_id: str


class StartSessionRequest(SessionRequest):
    title: str = ""


class SessionUpdateRequest(BaseModel):
    title: str | None = None
    is_pinned: bool | None = None


class LoopbackHostMiddleware:
    """Reject Host headers that could expose this UI outside its local origin."""

    def __init__(self, app: Any, *, port: int | None) -> None:
        self.app = app
        self._allowed_hosts = {"localhost", LOOPBACK_HOST}
        self._allowed_origins: set[str] = set()
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


def create_uvicorn_config(app: FastAPI, *, port: int) -> uvicorn.Config:
    """Return a Uvicorn configuration that never binds an external interface."""
    return uvicorn.Config(app, host=LOOPBACK_HOST, port=port)


def create_app(services: Services) -> FastAPI:
    """Create the loopback JSON API with only injectable local dependencies."""
    app = FastAPI()
    app.add_middleware(LoopbackHostMiddleware, port=services.loopback_port)
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
        remote = services.remote_factory(token)
        try:
            await remote.verify_token()
        except RemoteUnauthorizedError:
            services.credentials.delete_token()
            services.clear_authenticated()
            raise ApiError(401, "Authentication is required.") from None
        services.credentials.save_token(token)
        services.set_authenticated(token, remote)
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

    @app.get("/api/session-name")
    async def session_name() -> dict[str, str]:
        """Suggest a readable title for a session the user is about to start.

        The draft is named before the row exists so the title the user sees is
        the title that gets persisted, and so a meeting nobody renames is still
        findable in the history.
        """
        _require_authenticated(services)
        return {"title": services.session_titles()}

    @app.get("/api/devices")
    async def devices() -> dict[str, object]:
        return {"devices": _device_payloads(services)}

    @app.put("/api/devices/selection", status_code=204)
    async def save_device_selection(request: SessionRequest) -> Response:
        choices = _validated_choices(services.capture_backend, request)
        controller, _remote = _require_authenticated(services)
        if _capture_is_active(controller):
            raise ApiError(409, "Stop the active capture before changing audio devices.")
        services.save_selected_devices(choices)
        controller.restore_selected_devices(choices)
        return Response(status_code=204)

    @app.delete("/api/devices/selection", status_code=204)
    async def clear_device_selection() -> Response:
        controller, _remote = _require_authenticated(services)
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
        controller, _remote = _require_authenticated(services)
        if not microphone_id and not system_device_id:
            return _audio_level_response(services, test_choices=None)

        choices = _validated_choices(
            services.capture_backend,
            SessionRequest(microphone_id=microphone_id, system_device_id=system_device_id),
        )
        if _capture_is_active(controller):
            raise ApiError(409, "Stop the active capture before testing audio devices.")
        return _audio_level_response(services, test_choices=choices)

    @app.get("/api/sessions")
    async def list_sessions(request: Request) -> dict[str, object]:
        _controller, remote = _require_authenticated(services)
        cursor = request.query_params.get("cursor") or None
        query = (request.query_params.get("q") or "").strip()
        return _session_page_payload(await remote.list_sessions(cursor, query))

    @app.get("/api/sessions/{uuid_code}/segments")
    async def list_segments(request: Request, uuid_code: str) -> dict[str, object]:
        _controller, remote = _require_authenticated(services)
        cursor = request.query_params.get("cursor") or None
        return _segment_page_payload(await remote.list_segments(uuid_code, cursor))

    @app.patch("/api/sessions/{uuid_code}")
    async def update_session(uuid_code: str, request: SessionUpdateRequest) -> dict[str, object]:
        controller, remote = _require_authenticated(services)
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
        _controller, remote = _require_authenticated(services)
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
        choices = _validated_choices(services.capture_backend, request)
        controller, _remote = _require_authenticated(services)
        services.stop_audio_level_monitor()
        try:
            session = await controller.start_new(choices, title)
        except RuntimeError:
            raise ApiError(409, "The current session cannot be changed.") from None
        except DeviceUnavailableError:
            raise ApiError(422, "The selected capture device is unavailable.") from None
        services.save_selected_devices(choices)
        return _session_payload(session)

    @app.post("/api/sessions/{uuid_code}/resume", status_code=201)
    async def resume_session(uuid_code: str, request: SessionRequest) -> dict[str, object]:
        choices = _validated_choices(services.capture_backend, request)
        controller, remote = _require_authenticated(services)
        services.stop_audio_level_monitor()
        try:
            existing = await remote.get_session(uuid_code)
            session = await controller.resume(uuid_code, choices, title=existing.title)
        except RuntimeError:
            raise ApiError(409, "The current session cannot be changed.") from None
        except DeviceUnavailableError:
            raise ApiError(422, "The selected capture device is unavailable.") from None
        services.save_selected_devices(choices)
        return _session_payload(session)

    @app.post("/api/sessions/stop", status_code=204)
    async def stop_session() -> Response:
        controller, _remote = _require_authenticated(services)
        await controller.stop()
        return Response(status_code=204)

    @app.websocket("/api/events")
    async def events(websocket: WebSocket) -> None:
        """Push local state to the UI, starting with the bootstrap snapshot.

        The socket is accepted even without a credential: answering with an
        unauthenticated bootstrap is what lets the login screen render from this
        one channel instead of a second HTTP route repeating the same payload.
        """
        await websocket.accept()
        authenticated = services.authenticated()
        controller = authenticated[0] if authenticated is not None else None
        bootstrap = {"type": "bootstrap", "bootstrap": _bootstrap_payload(services, controller)}
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
        for task in (receive_task, event_task):
            task.cancel()
        await asyncio.gather(receive_task, event_task, return_exceptions=True)


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
    loop.create_task(closer())


def _remote_unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "The remote service is unavailable."})


def _require_authenticated(services: Services) -> tuple[DesktopSessionController, ListeningRemote]:
    authenticated = services.authenticated()
    if authenticated is None:
        raise ApiError(401, "Authentication is required.")
    return authenticated


def _delete_invalid_credential(services: Services) -> None:
    services.credentials.delete_token()
    services.clear_authenticated()


def _validated_choices(backend: CaptureBackend, request: SessionRequest) -> CaptureChoices:
    by_id = {device.device_id: device for device in backend.list_devices()}
    microphone = by_id.get(request.microphone_id)
    system = by_id.get(request.system_device_id)
    if microphone is None or microphone.kind != "mic" or system is None or system.kind != "system":
        raise ApiError(422, "Select an available microphone and system device.")
    return CaptureChoices(request.microphone_id, request.system_device_id)


def _choices_are_available(backend: CaptureBackend, choices: CaptureChoices) -> bool:
    devices = {device.device_id: device.kind for device in backend.list_devices()}
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


def _bootstrap_payload(
    services: Services, controller: DesktopSessionController | None
) -> dict[str, object]:
    choices = controller.selected_devices if controller is not None else None
    return {
        "authenticated": controller is not None,
        "devices": _device_payloads(services),
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


def _device_payloads(services: Services) -> list[dict[str, str]]:
    return [_device_payload(device) for device in services.capture_backend.list_devices()]


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
            services.stop_audio_level_test()


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
