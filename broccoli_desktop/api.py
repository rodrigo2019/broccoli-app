"""Loopback-only FastAPI API used by the embedded Broccoli Desktop UI."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from broccoli_desktop.capture import CaptureBackend, DeviceUnavailableError
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
from broccoli_desktop.remote import (
    ListeningRemote,
    RemoteProtocolError,
    RemoteRequestError,
    RemoteUnauthorizedError,
)
from broccoli_desktop.session import CaptureChoices, DesktopSessionController

LOOPBACK_HOST = "127.0.0.1"


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
    controller: DesktopSessionController | None = field(default=None, init=False)
    _remote: ListeningRemote | None = field(default=None, init=False, repr=False)
    _token: str | None = field(default=None, init=False, repr=False)

    def authenticated(self) -> tuple[DesktopSessionController, ListeningRemote] | None:
        """Return the controller and remote for the persisted token, if any."""
        token = self.credentials.load_token()
        if token is None:
            return None
        if self._token != token or self._remote is None or self.controller is None:
            remote = self.remote_factory(token)
            self._remote = remote
            self.controller = self.controller_factory(remote, self.capture_backend)
            self._token = token
        return self.controller, self._remote

    def set_authenticated(self, token: str, remote: ListeningRemote) -> None:
        """Install the already verified remote after credential persistence succeeds."""
        self._remote = remote
        self.controller = self.controller_factory(remote, self.capture_backend)
        self._token = token

    def clear_authenticated(self) -> None:
        """Forget in-memory authenticated dependencies after local credential deletion."""
        self._remote = None
        self.controller = None
        self._token = None


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


class UpdateTitleRequest(BaseModel):
    title: str


class LoopbackHostMiddleware:
    """Reject Host headers that could expose this UI outside its local origin."""

    def __init__(self, app: Any, *, port: int | None) -> None:
        self.app = app
        self._allowed_hosts = {"localhost", LOOPBACK_HOST}
        if port is not None:
            self._allowed_hosts.update({f"localhost:{port}", f"{LOOPBACK_HOST}:{port}"})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        host = next(
            (
                value.decode("latin-1")
                for key, value in scope.get("headers", [])
                if key.lower() == b"host"
            ),
            "",
        )
        if host.lower() in self._allowed_hosts:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"detail":"Local host required."}',
            }
        )


def create_uvicorn_config(app: FastAPI, *, port: int) -> uvicorn.Config:
    """Return a Uvicorn configuration that never binds an external interface."""
    return uvicorn.Config(app, host=LOOPBACK_HOST, port=port)


def create_app(services: Services) -> FastAPI:
    """Create the loopback JSON API with only injectable local dependencies."""
    app = FastAPI()
    app.add_middleware(LoopbackHostMiddleware, port=services.loopback_port)

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
        except (RemoteRequestError, RemoteProtocolError):
            raise ApiError(503, "The remote service is unavailable.") from None
        services.credentials.save_token(token)
        services.set_authenticated(token, remote)
        return Response(status_code=204)

    @app.delete("/api/login", status_code=204)
    async def logout() -> Response:
        controller = services.controller
        if controller is not None:
            await controller.stop()
        services.credentials.delete_token()
        services.clear_authenticated()
        return Response(status_code=204)

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict[str, object]:
        authenticated = services.authenticated()
        if authenticated is None:
            return _bootstrap_payload(None, SessionPage((), None))
        controller, remote = authenticated
        page = await _call_remote(services, lambda: remote.list_sessions(cursor=None, query=""))
        return _bootstrap_payload(controller, page)

    @app.get("/api/devices")
    async def devices() -> dict[str, object]:
        return {
            "devices": [
                _device_payload(device) for device in services.capture_backend.list_devices()
            ]
        }

    @app.get("/api/sessions")
    async def list_sessions(cursor: str | None = None, q: str = "") -> dict[str, object]:
        _controller, remote = _require_authenticated(services)
        page = await _call_remote(services, lambda: remote.list_sessions(cursor=cursor, query=q))
        return _session_page_payload(page)

    @app.get("/api/sessions/{uuid_code}")
    async def get_session(uuid_code: str) -> dict[str, object]:
        _controller, remote = _require_authenticated(services)
        session = await _call_remote(services, lambda: remote.get_session(uuid_code))
        return _session_payload(session)

    @app.get("/api/sessions/{uuid_code}/segments")
    async def list_segments(uuid_code: str, cursor: str | None = None) -> dict[str, object]:
        _controller, remote = _require_authenticated(services)
        page = await _call_remote(services, lambda: remote.list_segments(uuid_code, cursor))
        return _segment_page_payload(page)

    @app.patch("/api/sessions/{uuid_code}")
    async def update_title(uuid_code: str, request: UpdateTitleRequest) -> dict[str, object]:
        title = _validate_title(request.title)
        controller, _remote = _require_authenticated(services)
        try:
            session = await controller.update_title(uuid_code, title)
        except RemoteUnauthorizedError:
            _delete_invalid_credential(services)
            raise ApiError(401, "Authentication is required.") from None
        except (RemoteRequestError, RemoteProtocolError):
            raise ApiError(503, "The remote service is unavailable.") from None
        return _session_payload(session)

    @app.post("/api/sessions", status_code=201)
    async def start_session(request: StartSessionRequest) -> dict[str, object]:
        title = _validate_title(request.title)
        choices = _validated_choices(services.capture_backend, request)
        controller, _remote = _require_authenticated(services)
        try:
            session = await controller.start_new(choices, title)
        except RuntimeError:
            raise ApiError(409, "The current session cannot be changed.") from None
        except DeviceUnavailableError:
            raise ApiError(422, "The selected capture device is unavailable.") from None
        except RemoteUnauthorizedError:
            _delete_invalid_credential(services)
            raise ApiError(401, "Authentication is required.") from None
        except (RemoteRequestError, RemoteProtocolError):
            raise ApiError(503, "The remote service is unavailable.") from None
        return _session_payload(session)

    @app.post("/api/sessions/{uuid_code}/resume", status_code=201)
    async def resume_session(uuid_code: str, request: SessionRequest) -> dict[str, object]:
        choices = _validated_choices(services.capture_backend, request)
        controller, _remote = _require_authenticated(services)
        try:
            session = await controller.resume(uuid_code, choices)
        except RuntimeError:
            raise ApiError(409, "The current session cannot be changed.") from None
        except DeviceUnavailableError:
            raise ApiError(422, "The selected capture device is unavailable.") from None
        except RemoteUnauthorizedError:
            _delete_invalid_credential(services)
            raise ApiError(401, "Authentication is required.") from None
        except (RemoteRequestError, RemoteProtocolError):
            raise ApiError(503, "The remote service is unavailable.") from None
        return _session_payload(session)

    @app.post("/api/sessions/stop", status_code=204)
    async def stop_session() -> Response:
        controller, _remote = _require_authenticated(services)
        try:
            await controller.stop()
        except RemoteUnauthorizedError:
            _delete_invalid_credential(services)
            raise ApiError(401, "Authentication is required.") from None
        except (RemoteRequestError, RemoteProtocolError):
            raise ApiError(503, "The remote service is unavailable.") from None
        return Response(status_code=204)

    @app.websocket("/api/events")
    async def events(websocket: WebSocket) -> None:
        authenticated = services.authenticated()
        if authenticated is None:
            await websocket.close(code=1008)
            return
        controller, remote = authenticated
        try:
            page = await _call_remote(services, lambda: remote.list_sessions(cursor=None, query=""))
        except ApiError:
            await websocket.close(code=1008)
            return
        event_queue: asyncio.Queue[UiEvent] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def subscriber(event: UiEvent) -> None:
            loop.call_soon_threadsafe(event_queue.put_nowait, event)

        controller.events.subscribe(subscriber)
        await websocket.accept()
        try:
            await websocket.send_json(
                {"type": "bootstrap", "bootstrap": _bootstrap_payload(controller, page)}
            )
            await _send_events(websocket, event_queue)
        except WebSocketDisconnect:
            pass
        finally:
            controller.events.unsubscribe(subscriber)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root() -> HTMLResponse:
        return HTMLResponse("<!doctype html><title>Broccoli Desktop</title>")

    return app


async def _send_events(websocket: WebSocket, event_queue: asyncio.Queue[UiEvent]) -> None:
    while True:
        receive_task = asyncio.create_task(websocket.receive())
        event_task = asyncio.create_task(event_queue.get())
        done, pending = await asyncio.wait(
            {receive_task, event_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if event_task in done:
            await websocket.send_json(_event_payload(event_task.result()))
        if receive_task in done and receive_task.result()["type"] == "websocket.disconnect":
            return


async def _call_remote[RemoteResult](
    services: Services, operation: Callable[[], Awaitable[RemoteResult]]
) -> RemoteResult:
    try:
        return await operation()
    except RemoteUnauthorizedError:
        _delete_invalid_credential(services)
        raise ApiError(401, "Authentication is required.") from None
    except (RemoteRequestError, RemoteProtocolError):
        raise ApiError(503, "The remote service is unavailable.") from None


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


def _validate_title(title: str) -> str:
    try:
        return validate_title(title)
    except ValueError:
        raise ApiError(422, "The session title is invalid.") from None


def _bootstrap_payload(
    controller: DesktopSessionController | None, page: SessionPage
) -> dict[str, object]:
    choices = controller.selected_devices if controller is not None else None
    return {
        "authenticated": controller is not None,
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
        "sessions": _session_page_payload(page),
    }


def _device_payload(device: DeviceDescriptor) -> dict[str, str]:
    return {"device_id": device.device_id, "label": device.label, "kind": device.kind}


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
