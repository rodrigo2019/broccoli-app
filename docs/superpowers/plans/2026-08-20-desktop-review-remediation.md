# Desktop Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding from the 2026-08-20 review of `broccoli-app` — 3 Critical, 16 Important, 15 Minor — without changing the backend contract.

**Architecture:** Four phases in dependency order. Phase 1 puts a trust boundary on the loopback API (Origin check plus a per-launch capability token). Phase 2 replaces per-frame fire-and-forget tasks with one bounded queue and one sender task, and moves blocking device/credential I/O off the event loop. Phase 3 repairs packaging so CI produces a real build signal. Phase 4 covers the interface, localization, the proxy panel, and the documents.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, pywebview, pystray, PyAudioWPatch, soxr, websockets, httpx, keyring, pytest + pytest-asyncio, ruff 0.16.3, Tailwind/daisyUI 5, PyInstaller + Inno Setup.

**Spec:** `docs/superpowers/specs/2026-08-20-desktop-review-remediation-design.md`

## Global Constraints

- Python 3.12 only (`requires-python = ">=3.12,<3.13"`). Do not use syntax newer than 3.12.
- Run from the repo's own environment: `.venv\Scripts\python.exe`. The `python` on PATH is a different interpreter.
- Validation after any Python change, from the repo root: `.venv\Scripts\python.exe -m ruff format .`, then `-m ruff check --fix .`, then `-m ruff check .`. All three must succeed.
- Every text file is LF-only except `*.bat`. Never convert a file to CRLF.
- daisyUI 5: `form-control` does not exist. Use `fieldset` / `fieldset-legend` / `fieldset-label`.
- Tailwind is compiled at build time from `ui/app.css` and tree-shaken against its `@source` list. **A class name assembled at runtime in JS produces no CSS, silently.** Keep the existing lookup-table pattern (`NOTIFICATION_STYLES`, `CONNECTION_BADGE_TONES`).
- All remote-derived text reaches the DOM through `textContent`, never `innerHTML`.
- The backend token never enters a payload, a URL, a log, or the DOM.
- UI copy is Brazilian Portuguese. API `detail` strings stay English and are mapped in `app.js`.
- `AUDIO_QUEUE_MAX_FRAMES = 100`, which is `MAX_BUFFERED_AUDIO_MS // FRAME_DURATION_MS` (10_000 // 100). The two bounds must stay equal.
- Commit after every task. Never `--no-verify`.

---

## File Structure

**Phase 1**
- Modify `broccoli_desktop/api.py` — `LoopbackHostMiddleware` gains origin and token checks; `Services` gains `capability_token`; `create_uvicorn_config` gains a shutdown timeout; `POST /api/login` rejects during capture.
- Modify `broccoli_desktop/runtime.py` — generate the token, append it to the window URL.
- Modify `broccoli_desktop/static/app.js` — read the token from the URL, strip it, send it on every `/api/*` call.
- Modify `tests/visual_server.py` — fixed token for the visual runner.
- Modify `scripts/visual-check.ps1` — open the URL with the token.
- Modify `tests/test_api.py`, `tests/test_runtime.py`.

**Phase 2**
- Modify `broccoli_desktop/session.py` — the queue, the sender task, the task registry, the resume offset base.
- Modify `broccoli_desktop/api.py` — `asyncio.to_thread` wrappers, device cache.
- Modify `broccoli_desktop/events.py` — bound `_events`.
- Modify `broccoli_desktop/capture.py` — numpy `_pcm_level`.
- Modify `tests/test_session.py`, `tests/test_api.py`, `tests/test_capture.py`, `tests/fakes.py`.

**Phase 3**
- Modify `installer/BroccoliDesktop.spec`, `installer/BroccoliDesktop.iss`, `.github/workflows/ci.yml`, `CHANGELOG.md`, `scripts/visual-check.ps1`, `broccoli_desktop/tray.py`.
- Delete `tests/test_project.py`.
- Modify `tests/test_api.py` (remove source-grep assertions).

**Phase 4**
- Modify `broccoli_desktop/static/app.js`, `broccoli_desktop/static/index.html`, `ui/app.css`.
- Modify `broccoli_desktop/settings.py`, `broccoli_desktop/credentials.py`, `broccoli_desktop/remote.py`, `broccoli_desktop/api.py`, `broccoli_desktop/tray.py`, `broccoli_desktop/runtime.py`, `broccoli_desktop/naming.py`.
- Modify `README.md`, `docs/desktop-live-integration.md`, both specs under `docs/superpowers/specs/`.

---

# Phase 1 — Trust boundary of the local API

### Task 1: Reject foreign origins

**Files:**
- Modify: `broccoli_desktop/api.py:194-233` (`LoopbackHostMiddleware`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `LoopbackHostMiddleware.__init__(self, app, *, port: int | None)` keeps its signature. Behaviour added: a request carrying an `origin` header that is not `http://localhost:{port}` or `http://127.0.0.1:{port}` is rejected — HTTP `403` with body `{"detail":"Local origin required."}`, WebSocket close code `1008`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`, next to `test_host_header_must_name_the_local_loopback_service`:

```python
def test_a_foreign_origin_cannot_open_the_event_socket(client: TestClient) -> None:
    """A page on any other origin can reach ws://127.0.0.1:PORT because
    WebSockets are not subject to the same-origin policy. Only the Origin
    header separates the app's own window from that page."""
    login(client)

    with pytest.raises(WebSocketDisconnect) as rejection:
        with client.websocket_connect(
            "/api/events", headers={"origin": "http://evil.example"}
        ) as websocket:
            websocket.receive_json()

    assert rejection.value.code == 1008


def test_the_window_origin_still_opens_the_event_socket(client: TestClient) -> None:
    login(client)

    with client.websocket_connect(
        "/api/events", headers={"origin": "http://127.0.0.1:8765"}
    ) as websocket:
        assert websocket.receive_json()["type"] == "bootstrap"


def test_a_foreign_origin_cannot_reach_the_http_api(client: TestClient) -> None:
    login(client)

    response = client.get("/api/devices", headers={"origin": "http://evil.example"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Local origin required."}


def test_a_request_without_an_origin_is_allowed(client: TestClient) -> None:
    """Non-browser local callers -- the app's own httpx, a developer's curl --
    send no Origin at all."""
    login(client)

    assert client.get("/api/devices").status_code == 200
```

Add the imports the tests need at the top of `tests/test_api.py` if absent:

```python
import pytest
from starlette.websockets import WebSocketDisconnect
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -k origin -v`
Expected: FAIL. The foreign-origin cases pass through and return 200 / open the socket.

- [ ] **Step 3: Implement the origin check**

In `broccoli_desktop/api.py`, extend `LoopbackHostMiddleware.__init__`:

```python
    def __init__(self, app: Any, *, port: int | None) -> None:
        self.app = app
        self._allowed_hosts = {"localhost", LOOPBACK_HOST}
        self._allowed_origins: set[str] = set()
        if port is not None:
            self._allowed_hosts.update({f"localhost:{port}", f"{LOOPBACK_HOST}:{port}"})
            self._allowed_origins.update(
                {f"http://localhost:{port}", f"http://{LOOPBACK_HOST}:{port}"}
            )
```

Add a header reader and use it for both checks. Replace the body of `__call__` up to the host comparison:

```python
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

    async def _reject(
        self, scope: dict[str, Any], send: Any, *, status: int, detail: str
    ) -> None:
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
```

Ensure `import json` is present at the top of `api.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -v`
Expected: PASS, including the pre-existing `test_host_header_must_name_the_local_loopback_service`.

- [ ] **Step 5: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop/api.py tests/test_api.py
git commit -m "fix(desktop): reject foreign origins on the loopback API"
```

---

### Task 2: Require a per-launch capability token

**Files:**
- Modify: `broccoli_desktop/api.py` (`Services`, `LoopbackHostMiddleware`, `create_app`)
- Modify: `broccoli_desktop/runtime.py:150-151` (`url`), `:415` (window creation)
- Modify: `broccoli_desktop/static/app.js`
- Modify: `tests/visual_server.py`
- Modify: `scripts/visual-check.ps1`
- Test: `tests/test_api.py`, `tests/test_runtime.py`

**Interfaces:**
- Consumes: `LoopbackHostMiddleware._header` and `_reject` from Task 1.
- Produces:
  - `Services.capability_token: str | None = None`.
  - `LoopbackHostMiddleware.__init__(self, app, *, port: int | None, capability_token: str | None)`.
  - `LoopbackServer.url` unchanged; new `LoopbackServer.window_url` returning `f"{self.url}/?k={token}"`.
  - Header name `X-Broccoli-Key`; WebSocket query parameter `k`.
  - Visual runner token constant: `VISUAL_CAPABILITY_TOKEN = "visual-capability-token"` in `tests/visual_server.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
def test_the_api_requires_the_capability_token(tokened_client: TestClient) -> None:
    """Origin stops a web page. It does not stop another process on the machine,
    which sends no Origin at all -- that is what this token is for."""
    login(tokened_client, key="launch-key")

    without = tokened_client.get("/api/devices")
    with_key = tokened_client.get("/api/devices", headers={"X-Broccoli-Key": "launch-key"})

    assert without.status_code == 403
    assert without.json() == {"detail": "Local key required."}
    assert with_key.status_code == 200


def test_the_shell_and_its_assets_do_not_require_the_token(
    tokened_client: TestClient,
) -> None:
    """The page has to boot before it can present a key."""
    assert tokened_client.get("/").status_code == 200
    assert tokened_client.get("/static/app.js").status_code == 200


def test_the_event_socket_requires_the_capability_token(
    tokened_client: TestClient,
) -> None:
    login(tokened_client, key="launch-key")

    with pytest.raises(WebSocketDisconnect) as rejection:
        with tokened_client.websocket_connect("/api/events") as websocket:
            websocket.receive_json()

    assert rejection.value.code == 1008

    with tokened_client.websocket_connect("/api/events?k=launch-key") as websocket:
        assert websocket.receive_json()["type"] == "bootstrap"
```

Add the fixture next to the existing `client` fixture in `tests/test_api.py`:

```python
@pytest.fixture
def tokened_client(services: Services) -> Iterator[TestClient]:
    services.capability_token = "launch-key"
    with TestClient(create_app(services)) as client:
        yield client
```

Update the `login` helper in `tests/test_api.py` to accept an optional key:

```python
def login(client: TestClient, *, key: str | None = None) -> None:
    headers = {"X-Broccoli-Key": key} if key else {}
    response = client.post("/api/login", json={"token": "test-token"}, headers=headers)
    assert response.status_code == 204
```

Add to `tests/test_runtime.py`:

```python
def test_the_runtime_always_produces_a_capability_token() -> None:
    """The None escape hatch exists for the visual runner. If the production
    path could reach it, the hatch would be the hole."""
    server = LoopbackServer(_test_config(), port=None)

    assert server.capability_token
    assert len(server.capability_token) >= 32
    assert server.window_url.endswith(f"/?k={server.capability_token}")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -k capability tests/test_runtime.py -k capability -v`
Expected: FAIL — `Services` has no `capability_token`, `LoopbackServer` has no `window_url`.

- [ ] **Step 3: Implement the server side**

In `broccoli_desktop/api.py`, add the field to `Services`:

```python
    capability_token: str | None = None
```

Extend the middleware to take and enforce it:

```python
    def __init__(
        self, app: Any, *, port: int | None, capability_token: str | None = None
    ) -> None:
        ...
        self._capability_token = capability_token
```

and, after the origin check in `__call__`:

```python
        # /  and /static/* are the shell and its assets: no data, and the page
        # cannot present a key before it has loaded the script that reads one.
        if self._capability_token and scope.get("path", "").startswith("/api/"):
            presented = self._header(scope, b"x-broccoli-key")
            if not presented and scope["type"] == "websocket":
                presented = parse_qs(scope.get("query_string", b"").decode("latin-1")).get(
                    "k", [""]
                )[0]
            if not secrets.compare_digest(presented, self._capability_token):
                await self._reject(scope, send, status=403, detail="Local key required.")
                return
```

Add `import secrets` and `from urllib.parse import parse_qs` to `api.py`.

Pass it through in `create_app`:

```python
    app.add_middleware(
        LoopbackHostMiddleware,
        port=services.loopback_port,
        capability_token=services.capability_token,
    )
```

- [ ] **Step 4: Implement the runtime side**

In `broccoli_desktop/runtime.py`, generate the token in `LoopbackServer.__init__`:

```python
        self._capability_token = secrets.token_urlsafe(32)
```

Add `import secrets` and two properties next to `url`:

```python
    @property
    def capability_token(self) -> str:
        return self._capability_token

    @property
    def window_url(self) -> str:
        """The URL the native window opens. Carries the launch key once; app.js
        strips it from the address bar as soon as it has read it."""
        return f"{self.url}/?k={self._capability_token}"
```

Pass the token into `Services` wherever the runtime builds it, and change the window creation at `:415`:

```python
        window = create_window("Broccoli Desktop", server.window_url)
```

- [ ] **Step 5: Implement the client side**

At the top of `broccoli_desktop/static/app.js`, before any fetch happens:

```js
// The launch key arrives once, in the URL the native window opened. Read it,
// then strip it from the address bar so it is not sitting in a visible URL for
// the rest of the session. It never goes to storage and never leaves loopback.
const CAPABILITY_KEY = new URLSearchParams(window.location.search).get("k") || "";
if (CAPABILITY_KEY) {
  const clean = window.location.pathname + window.location.hash;
  window.history.replaceState(null, "", clean);
}

function apiHeaders(extra = {}) {
  return CAPABILITY_KEY ? { ...extra, "X-Broccoli-Key": CAPABILITY_KEY } : { ...extra };
}

function apiUrl(path) {
  return path;
}

function socketUrl(path) {
  const base = `ws://${window.location.host}${path}`;
  return CAPABILITY_KEY ? `${base}?k=${encodeURIComponent(CAPABILITY_KEY)}` : base;
}
```

Route every existing `fetch("/api/…")` through `apiHeaders(...)` for its `headers`, and every `new WebSocket("…/api/events")` and `new EventSource("/api/audio-levels/stream")` through `socketUrl(...)`. `EventSource` cannot set headers, so it uses the query parameter form as well.

- [ ] **Step 6: Keep the visual runner working**

In `tests/visual_server.py`:

```python
#: Fixed so scripts/visual-check.ps1 can open the page with a key. The
#: production path generates a random one per launch; test_runtime asserts it.
VISUAL_CAPABILITY_TOKEN = "visual-capability-token"
```

and pass `capability_token=VISUAL_CAPABILITY_TOKEN` into the `Services(...)` built by `create_visual_app`.

In `scripts/visual-check.ps1`, change the opening navigation:

```powershell
    Invoke-Browser -BrowserArguments @("open", "http://127.0.0.1:8765/?k=visual-capability-token")
```

- [ ] **Step 7: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS, all tests.

- [ ] **Step 8: Verify in the browser**

```bash
.venv/Scripts/python.exe -m tests.visual_server --port 8765
```

Then, in another shell, confirm the reproduced attack is now closed. Serve a page on a different origin that opens `ws://127.0.0.1:8765/api/events` with no key, and confirm it is refused rather than receiving a bootstrap. Expected: the socket closes with 1008 and no payload arrives.

- [ ] **Step 9: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop tests scripts/visual-check.ps1
git commit -m "fix(desktop): require a per-launch capability token on the local API"
```

---

### Task 3: Refuse login during capture, and bound shutdown

**Files:**
- Modify: `broccoli_desktop/api.py:99-105` (`set_authenticated`), `:313-327` (`POST /api/login`), `:236-238` (`create_uvicorn_config`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `_capture_is_active(controller)` — already present in `api.py`.
- Produces: `POST /api/login` returns `409` with `{"detail":"A capture is active."}` while a capture runs.

- [ ] **Step 1: Write the failing test**

```python
def test_login_during_a_capture_is_refused(client: TestClient, services: Services) -> None:
    """set_authenticated replaces the controller outright. The old one keeps two
    open WASAPI streams and a remote socket with nothing left holding a
    reference that can stop them -- the microphone stays open until the process
    exits."""
    login(client)
    start_capture(client)
    original = services.controller

    response = client.post("/api/login", json={"token": "another-token"})

    assert response.status_code == 409
    assert response.json() == {"detail": "A capture is active."}
    assert services.controller is original
```

If `start_capture` does not exist as a helper in `tests/test_api.py`, add it beside `login`:

```python
def start_capture(client: TestClient) -> None:
    client.post(
        "/api/settings",
        json={"microphone_id": "mic-1", "system_device_id": "system-1"},
    )
    response = client.post("/api/sessions", json={"title": "Reunião"})
    assert response.status_code == 200
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_login_during_a_capture_is_refused -v`
Expected: FAIL with `assert 204 == 409`.

- [ ] **Step 3: Implement**

In the `POST /api/login` handler, before `services.set_authenticated(...)`:

```python
        if _capture_is_active(services.controller):
            raise ApiError(status_code=409, detail="A capture is active.")
```

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -v`
Expected: PASS.

- [ ] **Step 5: Bound uvicorn's shutdown**

```python
def create_uvicorn_config(app: FastAPI, *, port: int) -> uvicorn.Config:
    """Return a Uvicorn configuration that never binds an external interface.

    The graceful-shutdown timeout is explicit because the default waits forever
    and the SSE meter stream never ends on its own.
    """
    return uvicorn.Config(app, host=LOOPBACK_HOST, port=port, timeout_graceful_shutdown=3)
```

- [ ] **Step 6: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop/api.py tests/test_api.py
git commit -m "fix(desktop): refuse login during capture and bound server shutdown"
```

---

# Phase 2 — Audio runtime core

### Task 4: One bounded queue, one sender task

**Files:**
- Modify: `broccoli_desktop/session.py:44-45` (constants), `:82` (state), `:448-467` (`_schedule_forward`, `_forward_frame`)
- Test: `tests/test_session.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: `AudioFrame`, `encode_audio_frame`, `ConnectionState`, `UiEvent`, `MAX_BUFFERED_AUDIO_MS`, `FRAME_DURATION_MS` — all already in `session.py`.
- Produces:
  - `AUDIO_QUEUE_MAX_FRAMES: int = MAX_BUFFERED_AUDIO_MS // FRAME_DURATION_MS`
  - `DesktopSessionController._audio_queue: asyncio.Queue[AudioFrame]`
  - `DesktopSessionController._sender_task: asyncio.Task | None`
  - `DesktopSessionController._dropped_frames: int`
  - A `UiEvent(type="warning", message="Áudio está sendo descartado: a conexão não está acompanhando.")` published on the first drop of a run.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_session.py`:

```python
async def test_a_stalled_socket_bounds_the_queue_instead_of_growing(
    controller: DesktopSessionController,
    remote: FakeSessionRemote,
) -> None:
    """Capture produces 20 frames/second/channel in real time. A socket that is
    slow rather than broken never reaches RECONNECTING, so the reconnect buffer
    never applies -- without a bound here, the frames just accumulate."""
    remote.stream.block_sends()
    await start_streaming(controller)

    for index in range(AUDIO_QUEUE_MAX_FRAMES * 3):
        controller._enqueue_frame(frame_at(index))
    await asyncio.sleep(0)

    assert controller._audio_queue.qsize() <= AUDIO_QUEUE_MAX_FRAMES
    assert controller._dropped_frames > 0


async def test_dropping_audio_tells_the_user(
    controller: DesktopSessionController,
    remote: FakeSessionRemote,
) -> None:
    remote.stream.block_sends()
    await start_streaming(controller)

    for index in range(AUDIO_QUEUE_MAX_FRAMES * 2):
        controller._enqueue_frame(frame_at(index))
    await asyncio.sleep(0)

    warnings = [event for event in controller.events.snapshot() if event.type == "warning"]
    assert any("descartado" in event.message for event in warnings)


async def test_frames_reach_the_remote_in_offset_order(
    controller: DesktopSessionController,
    remote: FakeSessionRemote,
) -> None:
    """One consumer means ordering by construction. With a task per frame they
    could interleave at send()'s drain point."""
    await start_streaming(controller)

    for index in range(20):
        controller._enqueue_frame(frame_at(index))
    await drain(controller)

    offsets = [decode_offset(payload) for payload in remote.stream.sent]
    assert offsets == sorted(offsets)


async def test_stop_leaves_no_task_running(
    controller: DesktopSessionController,
) -> None:
    await start_streaming(controller)

    await controller.stop()

    assert controller._sender_task is None
    assert not controller._tasks
```

Add the helpers this test module needs, near the top of `tests/test_session.py`:

```python
def frame_at(index: int) -> AudioFrame:
    return AudioFrame(channel="mic", offset_ms=index * FRAME_DURATION_MS, pcm=b"\x00\x01" * 1200)


def decode_offset(payload: bytes) -> int:
    """Read the offset back out of an encoded frame, so ordering is asserted on
    what the socket actually received rather than on what we think we sent."""
    return int.from_bytes(payload[4:12], "big")


async def drain(controller: DesktopSessionController) -> None:
    await controller._audio_queue.join()
```

Give `FakeSessionRemote`'s stream a way to stall, in `tests/fakes.py`:

```python
    def block_sends(self) -> None:
        """Stop acknowledging sends without closing, which is the slow-socket
        case: not an error, just never draining."""
        self._blocked = asyncio.Event()

    async def send_bytes(self, payload: bytes) -> None:
        if getattr(self, "_blocked", None) is not None:
            await self._blocked.wait()
        self.sent.append(payload)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session.py -k "queue or order or dropping or no_task" -v`
Expected: FAIL — `_enqueue_frame`, `_audio_queue`, `_dropped_frames`, `_sender_task` do not exist.

- [ ] **Step 3: Implement the queue and sender**

In `broccoli_desktop/session.py`, after the existing constants:

```python
#: The in-flight bound, deliberately equal to the reconnect buffer's: both are
#: "ten seconds of audio", and if they drift apart one of them is wrong.
AUDIO_QUEUE_MAX_FRAMES = MAX_BUFFERED_AUDIO_MS // FRAME_DURATION_MS
```

In `__init__`:

```python
        self._audio_queue: asyncio.Queue[AudioFrame] = asyncio.Queue(
            maxsize=AUDIO_QUEUE_MAX_FRAMES
        )
        self._sender_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._dropped_frames = 0
        self._reported_dropping = False
```

Replace `_schedule_forward` with an enqueue that runs on the loop:

```python
    def _schedule_forward(self, frame: AudioFrame) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._enqueue_frame, frame)

    def _enqueue_frame(self, frame: AudioFrame) -> None:
        """Hand one frame to the sender, dropping the oldest when the socket is
        not keeping up.

        Dropping is the honest failure here: the alternative is an unbounded
        queue that pins ~4.8 KB per frame at 20 frames/second/channel until the
        process dies. The user is told, because silent audio loss in a
        transcription product is worse than a visible gap.
        """
        while True:
            try:
                self._audio_queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                try:
                    self._audio_queue.get_nowait()
                    self._audio_queue.task_done()
                except asyncio.QueueEmpty:
                    return
                self._dropped_frames += 1
                if not self._reported_dropping:
                    self._reported_dropping = True
                    self.events.publish(
                        UiEvent(
                            type="warning",
                            message="Áudio está sendo descartado: a conexão não está acompanhando.",
                        )
                    )
```

Add the sender loop:

```python
    async def _send_audio_forever(self) -> None:
        """The only consumer of the audio queue.

        One consumer is what makes offset order a property of the code rather
        than of which task wins the drain, and it is what gives the queue a
        single place to apply backpressure.
        """
        while True:
            frame = await self._audio_queue.get()
            try:
                await self._forward_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[session] Failed to forward an audio frame")
            finally:
                self._audio_queue.task_done()
```

`_forward_frame` keeps its current body unchanged — it is now called from one place.

Start the sender when streaming begins (in `_open`, right after the stream is set) and register it:

```python
        self._sender_task = self._track(asyncio.create_task(self._send_audio_forever()))
```

- [ ] **Step 4: Add the task registry**

```python
    def _track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Hold a strong reference for the task's lifetime.

        CPython keeps only a weak reference to a running task, so a bare
        create_task can be collected mid-flight. For a frame that means silent
        audio loss; for _handle_device_loss it means a lost state transition.
        """
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
```

Route the two remaining bare `create_task` calls through it — `_on_capture_event`'s device-loss task at `session.py:474`, and `_release_remote` in `api.py:575`:

```python
                loop.call_soon_threadsafe(
                    lambda: self._track(asyncio.create_task(self._handle_device_loss()))
                )
```

- [ ] **Step 5: Cancel everything in `stop()`**

In `stop()`, replace the `_reader_task` cancellation with:

```python
        for task in (self._sender_task, self._reader_task):
            if task is not None:
                task.cancel()
        pending = [task for task in self._tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._sender_task = None
        self._reader_task = None
        self._tasks.clear()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session.py -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 8: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop/session.py broccoli_desktop/api.py tests/test_session.py tests/fakes.py
git commit -m "fix(desktop): send audio through one bounded queue and one sender task"
```

---

### Task 5: Move blocking device and credential I/O off the loop

**Files:**
- Modify: `broccoli_desktop/api.py:87-97` (`Services.authenticated`), `:451-462` (`_device_payloads`), `:594-600`, `:648`, `:697` (`_audio_level_events`)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Services.authenticated()` becomes `async def`; `_device_payloads` becomes `async def`; a module-level `DEVICE_CACHE_TTL_SECONDS = 5.0`.

- [ ] **Step 1: Write the failing test**

```python
def test_device_enumeration_is_cached_between_calls(
    client: TestClient, capture_backend: FakeCaptureBackend
) -> None:
    """Windows device enumeration routinely takes hundreds of milliseconds, and
    it ran on every /api/devices, every bootstrap and every session start --
    each one freezing the event socket and the meter stream with it."""
    login(client)

    client.get("/api/devices")
    client.get("/api/devices")

    assert capture_backend.list_devices_calls == 1
```

Add the counter to `FakeCaptureBackend` in `tests/fakes.py`:

```python
        self.list_devices_calls = 0

    def list_devices(self) -> list[DeviceDescriptor]:
        self.list_devices_calls += 1
        return list(self._devices)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_device_enumeration_is_cached_between_calls -v`
Expected: FAIL with `assert 2 == 1`.

- [ ] **Step 3: Implement the cache and the thread hops**

In `api.py`, add near the other constants:

```python
#: Long enough that a burst of calls costs one enumeration, short enough that
#: plugging in a headset shows up without a restart.
DEVICE_CACHE_TTL_SECONDS = 5.0
```

Add to `Services`:

```python
    _device_cache: tuple[float, list[DeviceDescriptor]] | None = field(
        default=None, init=False, repr=False
    )

    async def list_devices(self) -> list[DeviceDescriptor]:
        cached = self._device_cache
        now = time.monotonic()
        if cached is not None and now - cached[0] < DEVICE_CACHE_TTL_SECONDS:
            return cached[1]
        devices = await asyncio.to_thread(self.capture_backend.list_devices)
        self._device_cache = (now, devices)
        return devices

    async def authenticated(self) -> bool:
        """Read the Windows Credential Manager off the loop.

        This runs on every request, including each /api/events connect, and a
        vault read is not instant.
        """
        if self._token is not None:
            return True
        return await asyncio.to_thread(self.credentials.load_token) is not None
```

Add `import time` and `import asyncio` if absent. Update every caller of `services.authenticated()` and `_device_payloads(...)` to `await`. Wrap the remaining blocking calls:

- `CaptureSession.start()` invocation in `start_session`: `await asyncio.to_thread(controller.start_new, ...)` where the blocking `PyAudio.open` happens.
- The `finally` in `_audio_level_events`: `await asyncio.to_thread(services.stop_audio_level_test)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop/api.py tests/test_api.py tests/fakes.py
git commit -m "perf(desktop): keep device and credential I/O off the event loop"
```

---

### Task 6: Bound the event hub

**Files:**
- Modify: `broccoli_desktop/events.py:17,30,35`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `EVENT_HISTORY_MAX = 256`; `EventHub._events: deque[UiEvent]`; `snapshot()` keeps returning a list.

- [ ] **Step 1: Write the failing test**

```python
def test_the_event_hub_does_not_grow_without_bound() -> None:
    """Every delta and every segment passes through here. Unbounded, a
    four-hour meeting keeps the whole transcript in memory for a snapshot
    nothing in the product reads."""
    hub = EventHub()

    for index in range(EVENT_HISTORY_MAX * 2):
        hub.publish(UiEvent(type="info", message=f"event {index}"))

    assert len(hub.snapshot()) == EVENT_HISTORY_MAX
    assert hub.snapshot()[-1].message == f"event {EVENT_HISTORY_MAX * 2 - 1}"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session.py -k event_hub -v`
Expected: FAIL — `EVENT_HISTORY_MAX` is undefined.

- [ ] **Step 3: Implement**

```python
from collections import deque

#: snapshot() has one consumer, a test. Keeping a bounded tail preserves it
#: without retaining every transcript delta for the life of the process.
EVENT_HISTORY_MAX = 256
```

and in `EventHub.__init__`: `self._events: deque[UiEvent] = deque(maxlen=EVENT_HISTORY_MAX)`.

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop/events.py tests/test_session.py
git commit -m "fix(desktop): bound the event hub's retained history"
```

---

### Task 7: Resume from the right offset

**Files:**
- Modify: `broccoli_desktop/session.py:216-219`, `:511-513`
- Modify: `broccoli_desktop/remote.py` (add a last-page segment read)
- Test: `tests/test_session.py`, `tests/test_remote.py`

**Interfaces:**
- Consumes: `HttpListeningRemote.list_segments(uuid_code, cursor)` — extend if the method exists under another name; otherwise add it with this signature.
- Produces: `ListeningRemote.last_segment_offset_ms(uuid_code: str, segment_count: int) -> int`, returning `0` when `segment_count == 0`.
- `SEGMENT_PAGE_SIZE = 100` in `remote.py`, matching the backend's `DESKTOP_SEGMENT_PAGE_SIZE`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_resuming_a_stored_session_starts_after_its_last_segment(
    controller: DesktopSessionController,
    remote: FakeSessionRemote,
) -> None:
    """The base offset was only preserved for the session this controller
    already had open. Opening one from the library and pressing start gave base
    0, so new speech was written at offsets that already held the earlier part
    of the meeting."""
    remote.seed_segments("history-1", count=250, last_ended_offset_ms=1_240_000)

    await controller.start_new(choices(), resume_code="history-1", title=None)

    assert controller.pipeline_base_offset_ms == 1_240_000


def test_the_last_segment_page_lands_on_a_cursor_boundary() -> None:
    """The backend rejects any cursor that is not a multiple of the page size
    (views.py:61), so max(0, count - page_size) would be Invalid cursor for a
    250-segment session."""
    assert last_page_cursor(250, SEGMENT_PAGE_SIZE) == 200
    assert last_page_cursor(100, SEGMENT_PAGE_SIZE) == 0
    assert last_page_cursor(101, SEGMENT_PAGE_SIZE) == 100
    assert last_page_cursor(0, SEGMENT_PAGE_SIZE) == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_session.py -k resuming tests/test_remote.py -k cursor -v`
Expected: FAIL — `last_page_cursor` does not exist.

- [ ] **Step 3: Implement the cursor arithmetic**

In `broccoli_desktop/remote.py`:

```python
SEGMENT_PAGE_SIZE = 100


def last_page_cursor(segment_count: int, page_size: int) -> int:
    """The offset of the final page, on a page boundary.

    The backend validates cursors with `offset % page_size == 0`, so the
    obvious `count - page_size` is rejected for any count that is not a
    multiple of the page size.
    """
    if segment_count <= 0:
        return 0
    return ((segment_count - 1) // page_size) * page_size
```

and:

```python
    async def last_segment_offset_ms(self, uuid_code: str, segment_count: int) -> int:
        """Where a resumed session's new audio must start.

        Reads only the final page rather than walking the whole transcript.
        """
        if segment_count <= 0:
            return 0
        cursor = last_page_cursor(segment_count, SEGMENT_PAGE_SIZE)
        page = await self.list_segments(uuid_code, cursor=cursor)
        return max((segment["ended_offset_ms"] for segment in page), default=0)
```

- [ ] **Step 4: Use it on the resume path**

In `session.py`, initialise the attribute in `__init__`:

```python
        self._pipeline_base_offset_ms = 0
```

and replace the base computation at `:216-219`:

```python
        previous_session = (
            self._session if resume_code and self._session_uuid == resume_code else None
        )
        if previous_session and self._pipeline:
            previous_offset_ms = self._pipeline.next_offset_ms
        elif resume_code:
            # Resumed from the library: this controller has no history for it, so
            # the base has to come from what the backend already stored.
            previous_offset_ms = await self._remote.last_segment_offset_ms(
                resume_code, summary.segment_count
            )
        else:
            previous_offset_ms = 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop/session.py broccoli_desktop/remote.py tests/
git commit -m "fix(desktop): resume a stored session after its last stored offset"
```

---

### Task 8: Device loss during streaming, through the real backend

**Files:**
- Test: `tests/test_capture.py`
- Modify: `broccoli_desktop/capture.py:387-392` (`_pcm_level`)

**Interfaces:**
- Consumes: `PyAudioCaptureBackend`, `FakePyAudioStream` from `tests/fakes.py`.
- Produces: no new public interface.

- [ ] **Step 1: Write the failing test**

```python
def test_a_device_lost_mid_stream_reaches_the_controller_through_the_real_backend(
    pyaudio_double: FakePyAudio,
) -> None:
    """The paAbort / status_flags path is only ever exercised through
    FakePyAudioStream.emit today, so nothing covers the real backend translating
    a mid-stream device loss into a capture event."""
    backend = PyAudioCaptureBackend(pyaudio_factory=lambda: pyaudio_double)
    events: list[CaptureEvent] = []
    handle = backend.open(choices(), on_pcm=lambda *_: None, on_event=events.append)

    pyaudio_double.streams[0].raise_input_overflow_then_device_removed()

    handle.close()
    assert [event.type for event in events] == ["device_lost"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_capture.py -k mid_stream -v`
Expected: FAIL — the helper on the double does not exist yet; add it in `tests/fakes.py` to drive the real status-flag branch.

- [ ] **Step 3: Make it pass**

Implement `raise_input_overflow_then_device_removed` on the PyAudio double so it invokes the registered callback with the status flags the real backend inspects at `capture.py:88`. Do not change production behaviour unless the test proves it wrong — the point of this task is coverage of an existing path.

- [ ] **Step 4: Speed up `_pcm_level`**

```python
def _pcm_level(pcm: bytes) -> float:
    """RMS of a 16-bit little-endian block.

    numpy is already in the process via soxr; the pure-Python loop this replaces
    ran over ~96,000 samples/second across both channels.
    """
    if not pcm:
        return 0.0
    samples = numpy.frombuffer(pcm, dtype="<i2").astype(numpy.float32)
    return float(numpy.sqrt(numpy.mean(numpy.square(samples))) / 32768.0)
```

Add `import numpy` at the top of `capture.py`.

- [ ] **Step 5: Run the suite and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop/capture.py tests/
git commit -m "test(desktop): cover mid-stream device loss through the real backend"
```

---

# Phase 3 — Build integrity

### Task 9: Make the package build from a clean checkout

**Files:**
- Modify: `installer/BroccoliDesktop.spec:10,52`
- Modify: `.github/workflows/ci.yml`
- Delete: `tests/test_project.py`
- Modify: `tests/test_api.py:758-788`

**Interfaces:**
- Consumes: nothing.
- Produces: no `HOOKS_DIRECTORY`, no `hookspath` argument.

- [ ] **Step 1: Confirm the break**

```bash
git ls-files installer/
```
Expected: only `BroccoliDesktop.iss` and `BroccoliDesktop.spec`. Nothing under `installer/hooks/`, which `hookspath` points at — PyInstaller raises `FileNotFoundError` on a missing hook directory (`PyInstaller/depend/imphook.py:105`). It works locally only because a stale `__pycache__` keeps the directory alive.

- [ ] **Step 2: Remove the hookspath**

In `installer/BroccoliDesktop.spec`, delete the `HOOKS_DIRECTORY` constant at line 10 and the `hookspath=[...]` argument at line 52. The `Analysis(...)` call keeps every other argument unchanged.

- [ ] **Step 3: Delete the change-detector tests**

```bash
git rm tests/test_project.py
```

In `tests/test_api.py`, delete `test_static_client_renders_each_transcript_row_with_its_event_offset_timestamp` and any sibling assertion that greps `app.js` source text (lines 758-788). They pass if the code they describe is commented out, fail on any rename, and did not catch the break in the file they claim to test.

- [ ] **Step 4: Add a real packaging assertion to CI**

In `.github/workflows/ci.yml`, in the `package` job after the PyInstaller step:

```yaml
      - name: Verify the packaged tree carries its data files
        shell: pwsh
        run: |
          $root = "dist/BroccoliDesktop/_internal/broccoli_desktop"
          foreach ($required in @(
            "static/app.js",
            "static/index.html",
            "static/output.css",
            "static/vendor/bootstrap-icons/bootstrap-icons.css"
          )) {
            if (-not (Test-Path (Join-Path $root $required))) {
              throw "Packaged build is missing $required"
            }
          }
```

- [ ] **Step 5: Verify the build runs**

```bash
.\scripts\package.ps1
```
Expected: completes, and `dist/BroccoliDesktop/` exists with the files the CI step checks.

- [ ] **Step 6: Run the suite and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
git add installer/BroccoliDesktop.spec .github/workflows/ci.yml tests/test_api.py
git rm --cached tests/test_project.py 2>/dev/null; true
git commit -m "fix(desktop): repair packaging and drop the source-grep tests"
```

---

### Task 10: Version, changelog, icon, and script polish

**Files:**
- Modify: `tests/test_api.py` (version test), `CHANGELOG.md`, `broccoli_desktop/tray.py:96,98-103`, `installer/BroccoliDesktop.spec` (icon), `installer/BroccoliDesktop.iss:44,55-57`, `scripts/visual-check.ps1:113`

**Interfaces:**
- Consumes: `broccoli_desktop.static.images.broccoli_icon.svg`.
- Produces: `TrayController.__init__(self, ..., icon: Image.Image)` — the icon is injected instead of assigned to a private attribute from module scope.

- [ ] **Step 1: Read the version from pyproject**

Replace the hard-coded assertion:

```python
def test_package_exposes_a_version_matching_pyproject() -> None:
    """Hard-coding "0.1.0" made this fail on every version bump, which is a
    change detector, not a test."""
    manifest = tomllib.loads(Path(__file__).parent.parent.joinpath("pyproject.toml").read_text())

    assert broccoli_desktop.__version__ == manifest["project"]["version"]
```

Add `import tomllib` and `from pathlib import Path`.

- [ ] **Step 2: Fill in the changelog**

Under `## [Unreleased]` in `CHANGELOG.md`, add an `### Added` section listing: session pinning, session deletion, automatic session naming, audio level metering with a per-channel histogram, session search, and infinite scroll in the history. The repo declares Keep a Changelog; match the existing entry style.

- [ ] **Step 3: Ship the brand mark**

In `tray.py`, accept the icon rather than reaching into the controller:

```python
class TrayController:
    def __init__(self, ..., icon: Image.Image) -> None:
        ...
        self._icon = _PystrayIcon(icon)
```

and load the real mark instead of `Image.new("RGBA", (64, 64), "#1f8b4c")`, rendering `static/images/broccoli_icon.svg` to a PNG at build time. Point `installer/BroccoliDesktop.spec`'s `icon=` at the generated `.ico` rather than PyInstaller's default.

- [ ] **Step 4: Tidy the installer script**

In `installer/BroccoliDesktop.iss:44,55-57`, replace the doubled backslashes with single ones — `'SOFTWARE\Microsoft\EdgeUpdate\Clients'`. Windows collapses the repeated separators, so this changes nothing at runtime; it stops the next reader having to verify that.

- [ ] **Step 5: Accept a minimum agent-browser version**

In `scripts/visual-check.ps1`, replace the exact match:

```powershell
    $version = (& agent-browser --version).Trim()
    if ($LASTEXITCODE -ne 0) { throw "agent-browser is not available." }
    $parsed = [Version](($version -replace '^agent-browser\s+', '') -replace '-.*$', '')
    if ($parsed -lt [Version]"0.34.0") {
        throw "Expected agent-browser 0.34.0 or newer, received '$version'."
    }
```

- [ ] **Step 6: Run the suite and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add .
git commit -m "chore(desktop): version test, changelog, brand icon, script polish"
```

---

# Phase 4 — Interface, proxy, and documents

### Task 11: Focus management

**Files:**
- Modify: `broccoli_desktop/static/app.js`, `broccoli_desktop/static/index.html`, `ui/app.css`
- Modify: `scripts/visual-check.ps1`

**Interfaces:**
- Consumes: nothing.
- Produces: `focusScreen(container)` in `app.js`; a skip link with `data-testid="skip-to-main"` as the document's first focusable element.

- [ ] **Step 1: Add the helper**

```js
/** Move focus into a screen that just became visible.
 *
 * Every view change used to leave focus on <body>, so the next Tab restarted
 * from the top of a document with ~96 focusable elements -- about 82 of them
 * sidebar rows -- and a screen reader was told nothing had happened. The rename
 * dialog already does this correctly; this is the same behaviour everywhere
 * else. */
function focusScreen(container) {
  if (!container) return;
  const heading = container.querySelector("h1, h2");
  const target =
    heading ||
    container.querySelector("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
  if (!target) return;
  if (target === heading) target.setAttribute("tabindex", "-1");
  target.focus();
}
```

- [ ] **Step 2: Call it at every transition**

Call `focusScreen` after each view swap: login → capture, settings → capture, capture → settings, and after a session is opened from the sidebar. After a failed login, call `elements.tokenInput.focus()` instead.

- [ ] **Step 3: Patch the session list instead of rebuilding it**

The list is re-rendered from scratch on selection, which destroys the focused row — proven: after activating a row, the captured node reported `document.contains(node) === false` and focus was on `BODY`. Key each row by its `uuid_code`, reuse existing nodes, and only add, remove, or reorder what changed.

- [ ] **Step 4: Add the skip link**

As the first element inside `<body>` in `index.html`:

```html
<a href="#mainView" class="skip-link" data-testid="skip-to-main">Pular para o conteúdo</a>
```

and in `ui/app.css`, a class that keeps it off-screen until focused. Add the class name to a file the Tailwind `@source` list already scans, or write it as plain CSS in `app.css` — a runtime-assembled name would produce no CSS at all.

- [ ] **Step 5: Return focus from the row menu**

Replace `trigger.blur()` in `closeSessionMenu` (`app.js:870`) with `trigger.focus()`, so closing the menu returns to the row rather than dropping out of the list.

- [ ] **Step 6: Assert it in the visual check**

Add to `scripts/visual-check.ps1`, after the login step:

```powershell
    $focused = (Invoke-Browser -BrowserArguments @(
        "eval",
        "document.activeElement.tagName + '#' + document.activeElement.id"
    ) | ConvertFrom-Json)
    if ($focused -eq "BODY#") {
        throw "Focus was left on the body after entering the capture screen."
    }
```

Add the same assertion after opening a session from the sidebar.

- [ ] **Step 7: Rebuild the stylesheet and verify**

```bash
cd ui && npm run build && cd ..
.\scripts\visual-check.ps1
```
Expected: "Visual QA passed."

- [ ] **Step 8: Commit**

```bash
git add broccoli_desktop/static ui/app.css scripts/visual-check.ps1
git commit -m "fix(desktop): move focus into every screen that becomes visible"
```

---

### Task 12: Accessible names and heading structure

**Files:**
- Modify: `broccoli_desktop/static/index.html:132` and the screen headings
- Modify: `scripts/visual-check.ps1`

**Interfaces:**
- Consumes: nothing.
- Produces: no `aria-label` on `#tokenInput`; each screen has exactly one visible `<h1>`.

- [ ] **Step 1: Fix the token field**

```html
<fieldset class="fieldset">
  <legend class="fieldset-legend text-sm">Token de acesso</legend>
  <input id="tokenInput" class="input w-full" type="password" autocomplete="off"
         aria-describedby="tokenInputHelp" data-testid="token-input" required>
  <p id="tokenInputHelp" class="fieldset-label leading-snug">
    Guardado somente no Gerenciador de Credenciais do Windows.
  </p>
</fieldset>
```

The `aria-label="Broccoli access token"` is removed: it overrode the visible Portuguese `<legend>`, so a screen reader announced English while the screen showed Portuguese, and voice control could not address the field by the name the user reads (WCAG 2.5.3 Label in Name). axe did not flag it because the field did have *an* accessible name — just not the right one.

- [ ] **Step 2: Give each screen an `<h1>`**

The only `<h1>` today belongs to the hidden login screen, so axe reports `page-has-heading-one` on every post-login screen. Make the capture screen's "Transcrição" an `<h1>` and the settings screen's "Configurações" an `<h1>`, then demote the settings section headings so they sit *below* it — today the screen title is `<h3>` above `<h2>` sections, which is inverted.

- [ ] **Step 3: Split the live region**

Render provisional deltas into a container with `aria-live="off"` and move only finalized segments into the `role="log"`. A delta that grows as someone speaks currently makes a screen reader re-announce the whole utterance repeatedly.

- [ ] **Step 4: Assert it**

Extend the existing `Assert-NoAccessibilityViolations` calls to also run with `--tags best-practice`, and assert `page-has-heading-one` and `heading-order` are absent.

- [ ] **Step 5: Verify and commit**

```bash
cd ui && npm run build && cd ..
.\scripts\visual-check.ps1
git add broccoli_desktop/static scripts/visual-check.ps1
git commit -m "fix(desktop): correct accessible names and heading structure"
```

---

### Task 13: Layout and copy

**Files:**
- Modify: `broccoli_desktop/static/index.html`, `broccoli_desktop/static/app.js`, `ui/app.css`
- Modify: `scripts/visual-check.ps1`

**Interfaces:**
- Consumes: session payload fields `started_at`, `ended_at`, `title`.
- Produces: `formatSessionDate(startedAt)` and `formatSessionDuration(startedAt, endedAt)` in `app.js`.

- [ ] **Step 1: Give the title its space**

At 375px the title input measured **2 pixels** wide while `Código session-1 · 1 segmentos` took three lines. Restructure the header so the title keeps a usable minimum width at every viewport, and move the session code and segment count into the options menu or a tooltip.

- [ ] **Step 2: Assert it**

```powershell
    Invoke-Browser -BrowserArguments @("set", "viewport", "375", "812")
    $titleWidth = (Invoke-Browser -BrowserArguments @(
        "eval",
        "Math.round(document.querySelector('#sessionTitle').getBoundingClientRect().width)"
    ) | ConvertFrom-Json)
    if ($titleWidth -lt 120) {
        throw "The session title collapsed to ${titleWidth}px at 375px wide."
    }
    Invoke-Browser -BrowserArguments @("set", "viewport", "1440", "900")
```

- [ ] **Step 3: Put dates on the session rows**

```js
function formatSessionDate(startedAt) {
  return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short" }).format(
    new Date(startedAt),
  );
}

function formatSessionDuration(startedAt, endedAt) {
  if (!endedAt) return "";
  const minutes = Math.max(1, Math.round((new Date(endedAt) - new Date(startedAt)) / 60000));
  return `${minutes} min`;
}
```

Render both under the title in each row, and group rows by day. With auto-generated titles and 45 entries, the title alone does not let anyone find Tuesday morning's meeting — and the API already returns both fields.

- [ ] **Step 4: Fix the count copy**

```js
const segmentLabel = count === 1 ? "1 segmento" : `${count} segmentos`;
```

- [ ] **Step 5: Fire the reconnect toast on transition**

`renderConnectionState` (`app.js:1195`) notifies on every render while the state is `reconnecting`, so an outage produces a toast every 15 s forever. Keep the last rendered state and notify only when it changes.

- [ ] **Step 6: Stop clobbering the title being typed**

In `renderSessionDetails` (`app.js:1522`):

```js
  if (document.activeElement !== elements.sessionTitle) {
    elements.sessionTitle.value = session.title || "";
  }
```

- [ ] **Step 7: Announce the settings redirect**

When "Iniciar captura" redirects to settings for missing devices, write the reason into the settings screen's `role="status"` region and move focus to the microphone selector. Today it is a silent screen change — for a screen reader, indistinguishable from a dead button.

- [ ] **Step 8: Verify and commit**

```bash
cd ui && npm run build && cd ..
.\scripts\visual-check.ps1
git add broccoli_desktop/static ui/app.css scripts/visual-check.ps1
git commit -m "fix(desktop): title space, session dates, and copy corrections"
```

---

### Task 14: One language in the interface

**Files:**
- Modify: `broccoli_desktop/static/app.js`, `broccoli_desktop/tray.py:36-45,86-90`, `broccoli_desktop/runtime.py:97,105,405,427`
- Modify: `scripts/visual-check.ps1`

**Interfaces:**
- Consumes: API `detail` strings, which stay English.
- Produces: `API_MESSAGES` lookup in `app.js` mapping each `detail` to pt-BR, with a generic fallback.

- [ ] **Step 1: Add the lookup**

```js
// The API keeps returning machine-readable English detail strings -- that is the
// right contract for an API. The interface is pt-BR, so the mapping lives here.
const API_MESSAGES = {
  "The session title is invalid.": "O título da reunião não é válido.",
  "A capture is active.": "Já existe uma captura em andamento.",
  "Local host required.": "Esta ação só pode partir do aplicativo.",
  "Local origin required.": "Esta ação só pode partir do aplicativo.",
  "Local key required.": "Esta ação só pode partir do aplicativo.",
};

function translateApiMessage(detail) {
  return API_MESSAGES[detail] || "Não foi possível concluir a ação. Tente novamente.";
}
```

Route `reportError` (`app.js:643`) through it instead of showing `error.message` raw.

- [ ] **Step 2: Translate the tray and the dialogs**

`tray.py:36-45,86-90`: "Show Broccoli Desktop" → "Abrir o Broccoli Desktop", "Stop capture" → "Parar captura", "Quit" → "Sair". The status line between them already reads "Status: Transmitindo".

`runtime.py:97,105,405,427`: translate the Win32 dialog strings, including "A capture is active. Stop it and quit Broccoli Desktop?" → "Há uma captura em andamento. Deseja pará-la e sair do Broccoli Desktop?".

- [ ] **Step 3: Update the visual check**

`scripts/visual-check.ps1:236` waits on `"The session title is invalid."` in a pt-BR interface. Change it to `"O título da reunião não é válido."`.

- [ ] **Step 4: Verify and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
.\scripts\visual-check.ps1
git add broccoli_desktop scripts/visual-check.ps1
git commit -m "fix(desktop): one language across the interface, tray and dialogs"
```

---

### Task 15: Implement the proxy panel

**Files:**
- Modify: `broccoli_desktop/settings.py`, `broccoli_desktop/credentials.py`, `broccoli_desktop/remote.py:190,245-252`, `broccoli_desktop/api.py`
- Modify: `broccoli_desktop/static/index.html:216-251`, `broccoli_desktop/static/app.js:71-76,90,742,768`
- Test: `tests/test_settings.py`, `tests/test_credentials.py`, `tests/test_remote.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `CredentialStore.save_token` / `load_token` / `delete_token` patterns.
- Produces:
  - `ProxySettings(host: str, port: int, username: str, enabled: bool)` in `settings.py` — **no password field**.
  - `CredentialStore.save_proxy_password(password)` / `load_proxy_password()` / `delete_proxy_password()`.
  - `Services.proxy_url() -> str | None`, built as `http://user:pass@host:port` with the password read from the vault at call time.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_proxy_password_never_reaches_the_settings_payload(
    client: TestClient, services: Services
) -> None:
    """The panel used to persist the password to localStorage in clear text, in
    a product whose selling point is that credentials live in the Windows
    vault."""
    client.post(
        "/api/settings",
        json={"proxy": {"enabled": True, "host": "proxy.local", "port": 8080,
                        "username": "user", "password": "secret"}},
    )

    stored = client.get("/api/settings").json()

    assert "secret" not in json.dumps(stored)
    assert services.credentials.load_proxy_password() == "secret"


def test_clearing_the_proxy_deletes_the_stored_password(
    client: TestClient, services: Services
) -> None:
    client.post("/api/settings", json={"proxy": {"enabled": True, "host": "p", "port": 1,
                                                 "username": "u", "password": "secret"}})

    client.post("/api/settings", json={"proxy": {"enabled": False}})

    assert services.credentials.load_proxy_password() is None


async def test_the_remote_uses_the_configured_proxy() -> None:
    remote = HttpListeningRemote(base_url="https://example.invalid", token="t",
                                 proxy="http://user:pass@proxy.local:8080")

    assert remote.client_proxy == "http://user:pass@proxy.local:8080"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/ -k proxy -v`
Expected: FAIL — none of these exist.

- [ ] **Step 3: Implement storage**

`ProxySettings` in `settings.py` carries host, port, username and an `enabled` flag, and is persisted with the other device settings. The password goes to `CredentialStore` under its own key, reusing `_run_backend_operation` so no backend detail escapes into an error.

- [ ] **Step 4: Implement injection**

`Services.proxy_url()` returns `None` when the proxy is disabled. `HttpListeningRemote` passes it to `httpx.AsyncClient(proxy=…)` (`remote.py:245-252`) and `_WebSocketRemoteStream` to `websockets.connect(proxy=…)` (`remote.py:190`).

- [ ] **Step 5: Rework the panel**

Remove the demonstration copy from `index.html:216-251` ("Configure uma rota de proxy para esta demonstração", "Valores de exemplo pré-configurados para a demonstração"). Stop writing the proxy object into `persistSettings()` (`app.js:90`). Add a "Testar conexão" button that makes one request through the configured proxy and reports the result.

- [ ] **Step 6: Run the suite and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add broccoli_desktop tests
git commit -m "feat(desktop): implement the proxy settings for real"
```

---

### Task 16: Documents and remaining minors

**Files:**
- Modify: `README.md`, `docs/desktop-live-integration.md`, `docs/superpowers/specs/2026-08-19-broccoli-desktop-design.md`, `docs/superpowers/specs/2026-08-19-broccoli-desktop-live-integration-design.md`
- Modify: `broccoli_desktop/naming.py:122`, `broccoli_desktop/runtime.py:583-586`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing new.

- [ ] **Step 1: Correct the README**

Rewrite the sentence forbidding a remote endpoint in build inputs so it forbids tokens and transcripts only. The production hostname stays embedded in `config.py:12` — that is what the design spec intends, and the README was the document that was wrong.

- [ ] **Step 2: Update the stale specs**

In both design specs and `docs/desktop-live-integration.md`, replace the sections describing WebRTC VAD gating with continuous PCM at 24 kHz and server-side VAD, and remove the capability-gating language — history, remote title, resume and segment history are unconditionally available now. Record that the "open Broccoli web" action was dropped and only copy-code survives.

- [ ] **Step 3: Drop the module-level assert**

`naming.py:122`: delete it. It disappears under `python -O`, and `test_naming.py::test_every_title_the_lists_can_produce_is_a_valid_session_title` already covers the property.

- [ ] **Step 4: Retry the port probe**

`runtime.py:583-586` binds, reads the port, and closes, leaving a window before uvicorn binds. Retry once on `OSError` rather than surfacing the generic startup dialog.

- [ ] **Step 5: Final validation**

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m ruff format --check .
.\scripts\visual-check.ps1
.\scripts\package.ps1
```
Expected: tests pass, lint clean, "Visual QA passed.", and the installer builds.

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "docs(desktop): reconcile the specs and README with the shipped app"
```

---

## Self-Review

**Spec coverage.** Phase 1 → Tasks 1-3 (C1, I6, uvicorn timeout). Phase 2 → Tasks 4-8 (C3, I1, I2, I3, I8's device-loss gap, `_pcm_level`). Phase 3 → Tasks 9-10 (C2, I7, changelog, icon, `.iss`, version pin). Phase 4 → Tasks 11-16 (focus, names, layout, localization, proxy, documents). Every numbered finding in the spec maps to a task.

**Type consistency.** `capability_token` is the field name in `Services`, the parameter name in `LoopbackHostMiddleware`, and the property on `LoopbackServer` throughout. The header is `X-Broccoli-Key` and the query parameter is `k` in every task that touches them. `AUDIO_QUEUE_MAX_FRAMES` is defined once in Task 4 and referenced by that name in its tests. `last_page_cursor` and `SEGMENT_PAGE_SIZE` are defined in Task 7 and used only there.

**Known gap.** Tasks 11-15 change JavaScript, and this repository has no JS test infrastructure. Their verification is `scripts/visual-check.ps1`, which drives the real DOM — each of those tasks extends it rather than asserting on source text, which is the failure mode Task 9 deletes.
