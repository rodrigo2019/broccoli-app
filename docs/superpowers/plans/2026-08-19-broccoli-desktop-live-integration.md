# Broccoli Desktop Live Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adapt Broccoli Desktop to the existing Listening token/WebSocket contract and prove the complete local platform-to-desktop audio flow.

**Architecture:** The desktop owns a thin adapter around the backend's existing `GET /api/auth/me/` and `/ws/listening/` contract. Its local FastAPI exposes only supported desktop capabilities and tells the UI which history features are unavailable. A browser-only real runtime enables agent-browser to test the platform and desktop without changing the backend.

**Tech Stack:** Python 3.12, FastAPI, PyWebView runtime abstractions, websockets 17, PyAudioWPatch/WASAPI, Django local runserver, agent-browser headed Chrome.

**Spec:** `docs/superpowers/specs/2026-08-19-broccoli-desktop-live-integration-design.md`

## Global Constraints

- Modify only `C:\repos\broccoli-app`; do not edit, format, migrate, commit, or add tests in `C:\repos\broccoli`.
- Treat `C:\repos\broccoli` as the authoritative backend contract: no endpoint is invented and no backend route changes for desktop convenience.
- Keep token, password, audio, PCM, transcript text and HAR out of logs, Git, screenshots and reports.
- Retain the real local token, credential, ListeningSession and TranscriptSegment after accepted manual integration.
- Use test-first Red/Green evidence for every production behavior change.
- Use a named agent-browser session, headed browser, no profile/restore/HAR, and domain allowlisting for manual live acceptance.

---

### Task 1: Map the remote adapter to the declared Listening contract

**Files:**
- Modify: `broccoli_desktop/remote.py`, `broccoli_desktop/session.py`
- Modify: `tests/test_remote.py`, `tests/test_session.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: `GET /api/auth/me/`, `WS /ws/listening/?resume=&device=&language=` and the version-1 binary frame codec.
- Produces: `ListeningRemote.verify_token() -> None`, `SessionStarted(uuid_code, next_sequence_by_channel, max_duration_s)`, final segments with a locally derived utterance ID, and typed close-code exceptions.

- [ ] **Step 1: Write failing remote contract tests**

```python
@pytest.mark.asyncio
async def test_verify_token_uses_the_declared_token_endpoint(fake_transport):
    remote = HttpListeningRemote("http://127.0.0.1:8000", "secret", websocket_path="/ws/listening/")

    await remote.verify_token()

    assert fake_transport.requests == [("GET", "/api/auth/me/", "Token secret")]


@pytest.mark.asyncio
async def test_stream_uses_handshake_query_and_derives_segment_id(fake_socket_factory):
    fake_socket_factory.socket.received = [
        json.dumps({"type": "session.started", "uuid_code": "live-1", "resumed": False,
                    "next_seq": {"mic": 4, "system": 2}, "max_duration_s": 14400}),
        json.dumps({"type": "transcript.segment", "channel": "mic", "text": "hello",
                    "started_offset_ms": 100, "ended_offset_ms": 900}),
    ]
    remote = HttpListeningRemote("http://127.0.0.1:8000", "secret", websocket_path="/ws/listening/")

    stream = await remote.connect_stream(resume_code=None, device_label="Speakers", language="en")
    events = [event async for event in stream.events()]

    assert fake_socket_factory.url == "ws://127.0.0.1:8000/ws/listening/?device=Speakers&language=en"
    assert fake_socket_factory.socket.sent == []
    assert events[1] == TranscriptSegmentEvent("mic", "mic:4", "hello", 100, 900)
```

- [ ] **Step 2: Run the focused tests and confirm Red**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_remote.py -q`

Expected: failures identify the legacy sessions endpoint, `session.start` control, and unsupported event fields.

- [ ] **Step 3: Implement the minimal adapter changes**

- Change token validation to a successful JSON response from `/api/auth/me/`.
- Build the WebSocket URL with `urlencode({"resume": ..., "device": ..., "language": "en"})`, omitting empty values.
- Remove initial `send_control({"type": "session.start", ...})`.
- Store `next_seq` by channel after `session.started`; derive each final segment ID before incrementing that channel's counter.
- Convert WebSocket code 4401 to `RemoteUnauthorizedError`; expose 4402 and 4403 as client-safe remote failures.
- For new capture initialize the audio pipeline at zero. For recovery, retain the pre-existing local pipeline and replay its bounded frames without resetting its offset.
- Build the active `SessionSummary` from the start event and local fields; do not fetch or patch a remote title.

- [ ] **Step 4: Add controller regression tests and verify Green**

```python
@pytest.mark.asyncio
async def test_recovery_keeps_local_offsets_when_backend_omits_next_offset_ms(controller, live_remote):
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="")
    controller.enqueue_audio_frames([AudioFrame("system", 600, b"\x00\x00")])
    await live_remote.close_current_stream()
    await drain_recovery(controller)

    assert decode_audio_frame(live_remote.streams[-1].frames[0]).offset_ms == 600
```

Run: `./.venv/Scripts/python.exe -m pytest tests/test_remote.py tests/test_session.py -q`

Expected: all focused tests pass, with no HTTP request to a session-library endpoint.

- [ ] **Step 5: Commit the adapter**

```powershell
git add broccoli_desktop/remote.py broccoli_desktop/session.py tests/test_remote.py tests/test_session.py tests/fakes.py
git commit -m "fix: align desktop remote adapter with listening contract"
```

### Task 2: Publish capabilities and make the notebook honest about unavailable history

**Files:**
- Modify: `broccoli_desktop/api.py`, `broccoli_desktop/static/index.html`, `broccoli_desktop/static/app.js`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: the reduced `ListeningRemote` and active local `SessionSummary` from Task 1.
- Produces: bootstrap `capabilities`, supported local routes, `409` capability responses, and a live-capture-only notebook state.

- [ ] **Step 1: Write failing API and static-contract tests**

```python
def test_bootstrap_marks_history_features_unavailable(client):
    login(client)

    response = client.get("/api/bootstrap")

    assert response.json()["capabilities"] == {
        "history": False,
        "remote_title": False,
        "user_resume": False,
        "segment_history": False,
    }


def test_notebook_explains_history_is_unavailable():
    source = Path("broccoli_desktop/static/app.js").read_text(encoding="utf-8")

    assert "Histórico não disponível neste backend" in source
    assert 'capabilities.history' in source
```

- [ ] **Step 2: Run the focused tests and confirm Red**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_api.py -q`

Expected: bootstrap lacks capabilities and the static source still loads history unconditionally.

- [ ] **Step 3: Implement only supported routes and presentation**

- Bootstrap and event bootstrap return an empty local session page plus the four fixed capability booleans.
- Remove remote history calls from bootstrap and event connection.
- Return `409 {"detail": "This backend does not provide session history."}` from legacy history, segment, title and user-resume routes if invoked; do not call the backend.
- Keep `/api/sessions` POST, `/api/sessions/stop`, `/api/devices`, login, logout and `/api/events`.
- Make the UI disable history controls and display the agreed Portuguese explanation. The active code copy remains available; title is visibly local-only and never triggers PATCH.
- Keep the timeline handling for final segments and local state/status events.

- [ ] **Step 4: Verify Green and rebuild CSS**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py -q
.\scripts\build-css.ps1
```

Expected: tests pass and the generated stylesheet includes any newly literal capability-state classes.

- [ ] **Step 5: Commit capability UI**

```powershell
git add broccoli_desktop/api.py broccoli_desktop/static/index.html broccoli_desktop/static/app.js tests/test_api.py ui/app.css
git commit -m "feat: expose listening backend capabilities in desktop"
```

### Task 3: Add the real loopback browser-only runtime

**Files:**
- Modify: `broccoli_desktop/runtime.py`, `scripts/run.ps1`
- Create: `broccoli_desktop/browser_only.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**
- Consumes: `RuntimeConfig`, real `Services`, Uvicorn loopback server and the configured backend WebSocket path.
- Produces: `python -m broccoli_desktop.browser_only --local --port <port>` and `scripts/run.ps1 -BrowserOnly -Port <port>` without PyWebView or tray.

- [ ] **Step 1: Write failing browser-only lifecycle tests**

```python
def test_browser_only_starts_the_real_loopback_server_at_the_requested_port(fake_server_factory):
    server = start_browser_only(config, port=8765, server_factory=fake_server_factory)

    assert server.url == "http://127.0.0.1:8765"
    assert fake_server_factory.started is True
    assert fake_server_factory.window_created is False
```

- [ ] **Step 2: Run the focused test and confirm Red**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_runtime.py -q`

Expected: import or requested-port assertion fails because the real browser-only entry point does not exist.

- [ ] **Step 3: Implement the browser-only entry point**

- Allow `UvicornLoopbackServer` to accept an explicit validated loopback port while retaining dynamic allocation for normal desktop startup.
- Add `start_browser_only` that creates the same production services, starts and health-checks the server, and blocks until interruption; its `finally` always stops capture/server.
- Add `browser_only.py` argument parsing for `--local`, `--dev` and `--port`; reuse `parse_runtime_config` rather than duplicate environment selection.
- Change `scripts/run.ps1`: `-FakeRemote -BrowserOnly` retains the visual fake server; `-BrowserOnly` alone starts the real browser-only runner; default still launches PyWebView.

- [ ] **Step 4: Verify Green**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_config.py -q
.\.venv\Scripts\ruff.exe format --check broccoli_desktop/runtime.py broccoli_desktop/browser_only.py tests/test_runtime.py
.\.venv\Scripts\ruff.exe check broccoli_desktop/runtime.py broccoli_desktop/browser_only.py tests/test_runtime.py
```

Expected: browser-only starts/stops deterministically and never constructs a native window or tray.

- [ ] **Step 5: Commit the runtime mode**

```powershell
git add broccoli_desktop/runtime.py broccoli_desktop/browser_only.py scripts/run.ps1 tests/test_runtime.py
git commit -m "feat: add real desktop browser-only runtime"
```

### Task 4: Preserve fake visual QA and add the live integration procedure

**Files:**
- Modify: `.gitignore`, `scripts/visual-check.ps1`, `tests/visual_server.py`, `tests/test_api.py`
- Create: `scripts/live-integration.ps1`, `docs/desktop-live-integration.md`

**Interfaces:**
- Consumes: fake browser-only route for deterministic visual QA and Task 3's real browser-only route for manual acceptance.
- Produces: fake QA that reflects capability UI and a PowerShell runner that starts/stops local backend/app processes without knowing credentials.

- [ ] **Step 1: Write failing visual/static tests**

```python
def test_fake_bootstrap_exposes_the_same_capability_shape(client):
    login_with_visual_token(client)

    assert client.get("/api/bootstrap").json()["capabilities"]["history"] is False
```

- [ ] **Step 2: Run the focused test and confirm Red**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_api.py -q`

Expected: fake data still models the removed history workflow.

- [ ] **Step 3: Update fake QA and add a non-secret live runner**

- Make visual fakes exercise login, device selection, start, final segment, stop and unavailable-history state only.
- Update `visual-check.ps1` assertions and screenshots to use only fake token/transcript data and verify no console error, no token text and empty web storage.
- Add `artifacts/integration/` to `.gitignore` before the live runner can create its sanitized acceptance evidence.
- Create `live-integration.ps1` that verifies local virtual environments, starts `C:\repos\broccoli\broccoli\manage.py runserver 127.0.0.1:8000`, waits for a non-authenticated healthable platform response, sets only the non-secret `/ws/listening/` configuration in the child desktop process, starts real browser-only at a caller-selected loopback port, and terminates both child process trees in `finally`.
- Document the manual agent-browser sequence, the no-screenshot token phase, preconditions, expected timeline result, persistence query and sanitized reporting format. The script must never contain credentials, token values, YouTube transcript text or cleanup that deletes retained data.

- [ ] **Step 4: Verify deterministic visual acceptance**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py -q
.\scripts\visual-check.ps1
```

Expected: fake-only visual QA passes with only the live-capture controls and no stored fake credential.

- [ ] **Step 5: Commit acceptance tooling**

```powershell
git add .gitignore scripts/visual-check.ps1 scripts/live-integration.ps1 tests/visual_server.py tests/test_api.py docs/desktop-live-integration.md
git commit -m "test: prepare desktop live integration acceptance"
```

### Task 5: Run authenticated platform and desktop acceptance with agent-browser

**Files:**
- Create ignored: `artifacts/integration/live-<timestamp>/report.md`
- Create ignored only if needed: sanitized screenshots without token or transcript text

**Interfaces:**
- Consumes: Task 4 runner, the local `admin` account, profile token UI, real desktop browser-only UI and a WASAPI loopback device.
- Produces: an evidence-backed acceptance report; no repository code is changed in this task.

- [ ] **Step 1: Start the controlled local processes**

Run:

```powershell
.\scripts\live-integration.ps1 -DesktopPort 8765
```

Expected: backend at `127.0.0.1:8000` and desktop browser-only UI at `127.0.0.1:8765`; if Azure, credit, user/tenant or WASAPI prerequisites fail, record the exact non-secret precondition and stop.

- [ ] **Step 2: Create and copy a local token without exposing it**

Use one named headed agent-browser session with only localhost and required YouTube domains allowed. Visit `http://127.0.0.1:8000/admin/`, log in with the user-supplied local credentials, then visit `/profile/`. Create `desktop-integration-<timestamp>` with validity `30`, click the UI copy control, close the token modal, and do not snapshot, read DOM text, call `get value`, write a HAR, or record video while the value is visible.

- [ ] **Step 3: Authenticate and begin capture in the desktop UI**

- Open `http://127.0.0.1:8765/` in another tab of the same browser session.
- Focus the token input and send `Control+V`; click Entrar; wait for the capture UI and confirm the input value is empty without printing it.
- Select the first available microphone and first available system-loopback device by their visible option labels; do not record opaque device IDs.
- Verify the history-unavailable explanation and start capture.

- [ ] **Step 4: Exercise system-audio transcription**

- Open `https://www.youtube.com/watch?v=iCvmsMzlF7o` in a third tab and play it headed for at least 60 seconds.
- Return to desktop tab; wait up to 90 seconds for a non-empty timeline row and `Transmitindo` state.
- Stop capture; collect browser errors JSON and a11y JSON, failing the acceptance on console errors or Axe violations. Do not persist a screenshot containing token or transcript text.

- [ ] **Step 5: Verify persistence and report outcome**

Read only aggregate local database facts through `manage.py shell`: newest admin ListeningSession UUID, status and segment count. Write the ignored report with time window, selected channel availability, state transition, UUID, count, console/a11y result and any external precondition. Keep the token, Credential Manager entry, session and segments.

## Plan self-review

- **Spec coverage:** Task 1 maps every authentication/WebSocket/frame/event difference. Task 2 maps capabilities into API and UI. Task 3 creates the testable real runtime. Task 4 preserves fake visual coverage and starts controlled processes. Task 5 performs the requested platform plus app agent-browser acceptance.
- **No-backend scope:** Every created/modified path is in `C:\repos\broccoli-app`; backend commands are runtime-only and no backend file, migration or test is listed.
- **Placeholder scan:** Commands, expected behavior, interfaces and retained-data policy are defined; external preconditions are explicit gates rather than deferred implementation.
- **Type consistency:** Task 1 produces the reduced remote/session contract consumed by Task 2. Task 3 exposes the runtime consumed by Task 4, and Task 5 uses only that runner.
