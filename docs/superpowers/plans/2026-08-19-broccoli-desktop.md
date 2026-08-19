# Broccoli Desktop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Deliver Broccoli Desktop, an installable Windows 11 desktop client that captures meeting audio, streams it through the external Listening contract, and presents live and historical transcripts.

**Architecture:** A Python process starts a loopback-only FastAPI service and opens its static daisyUI interface in PyWebView. The FastAPI layer owns Windows credentials, selected audio devices, capture, VAD, WebSocket recovery, and an event hub; the browser UI calls only this local API. The remote Broccoli API and WebSocket are external prerequisites consumed through a typed adapter and fakes.

**Tech Stack:** Python 3.12.10, FastAPI 0.141.1, Uvicorn 0.52.4, PyWebView 6.2.1, websockets 17.0.1, PyAudioWPatch 0.2.12.8, webrtcvad-wheels 2.0.14, soxr 1.1.0, keyring 25.7.0, pystray 0.19.5, PyInstaller 6.22.2, Tailwind 4, daisyUI 5, pytest, Ruff, Inno Setup.

**Spec:** docs/superpowers/specs/2026-08-19-broccoli-desktop-design.md

## Global Constraints

- Work only in C:\repos\broccoli-app. Do not create, edit, test, migrate, or deploy anything in C:\repos\broccoli.
- The remote Listening REST and WebSocket contract in the spec is an external prerequisite; test the desktop app with fakes until that environment is supplied.
- Target Windows 11 x64 and Python 3.12.10 only.
- Store the token only in Windows Credential Manager. Do not write tokens, PCM data, transcript copies, or remote payloads to files or logs.
- The local FastAPI server binds only to 127.0.0.1. The web UI never receives the API token.
- Use 48 kHz, 20 ms capture blocks; WebRTC VAD; mono PCM16 at 24 kHz; and 100 ms remote frames.
- Use the exact external REST paths and WebSocket event shapes in the approved spec.
- Use LF-only files, committed uv.lock, committed package-lock.json, Ruff, and pytest.
- Every task is test-first and ends with a focused commit. Do not use real microphone hardware, remote APIs, Azure, or Windows Credential Manager in automated tests.
- Use `agent-browser` 0.34.0 for visual QA; provision its matching Chromium binary with `agent-browser install` rather than adding it to the desktop application's shipped dependencies.
- Run browser-facing visual QA with the `agent-browser` skill against only the fake remote at `http://127.0.0.1`; always use an isolated worktree-scoped browser session, never a shared browser session, a real token, a profile, restore state, HAR capture, or a remote Broccoli URL.
- Keep agent-browser screenshots under `artifacts/visual/`, review them before accepting the UI, and never commit those artifacts because they can contain fake transcript text.

## Locked file structure

~~~text
broccoli_desktop/
  __init__.py                 Package version
  __main__.py                 CLI entry point
  config.py                   Production, --dev, and --local URL selection
  models.py                   Shared local and remote-facing Pydantic/dataclass types
  credentials.py              Keyring adapter and testable protocol
  remote.py                   External REST and WebSocket adapter
  protocol.py                 Listening binary frame encoder
  audio.py                    VAD, resampling, and 100 ms framing
  capture.py                  PyAudioWPatch device and stream adapter
  events.py                   Local UI event hub and event types
  session.py                  Capture lifecycle, recovery, and remote coordination
  api.py                      Loopback FastAPI application
  runtime.py                  Uvicorn, PyWebView, and process lifecycle
  tray.py                     Pystray controller
  static/
    index.html                Caderno de reunião shell
    app.js                    Browser state and local API client
    output.css                Generated, ignored Tailwind output
tests/
  conftest.py
  fakes.py
  test_project.py
  test_config.py
  test_credentials.py
  test_remote.py
  test_protocol.py
  test_audio.py
  test_capture.py
  test_session.py
  test_api.py
  test_runtime.py
  test_tray.py
ui/
  app.css                     Tailwind input and daisyUI themes
  package.json
  package-lock.json
scripts/
  sync.ps1
  run.ps1
  check.ps1
  build-css.ps1
  package.ps1
  installer.ps1
installer/
  BroccoliDesktop.spec        PyInstaller specification
  BroccoliDesktop.iss         Inno Setup installer
.github/workflows/
  ci.yml
~~~

---

### Task 1: Bootstrap the Windows desktop repository

**Files:**
- Create: pyproject.toml, .python-version, ruff.toml, .editorconfig, .gitattributes, .gitignore, CHANGELOG.md, README.md
- Create: broccoli_desktop/__init__.py, tests/__init__.py, tests/test_project.py
- Create: ui/package.json, ui/app.css, scripts/sync.ps1, scripts/check.ps1
- Modify: none

**Interfaces:**
- Consumes: Python 3.12.10 and an externally provisioned uv executable.
- Produces: the broccoli-desktop console script, frozen Python environment, and Tailwind/daisyUI source configuration.

- [ ] **Step 1: Write the failing project-metadata test**

~~~python
from importlib.metadata import version

def test_package_exposes_the_initial_release_version():
    assert version("broccoli-desktop") == "0.1.0"
~~~

- [ ] **Step 2: Add the project metadata and package scaffold**

Create pyproject.toml with Hatchling 1.31.0, name broccoli-desktop, version 0.1.0, requires-python >=3.12,<3.13, the pinned runtime dependencies from the spec, and a dev group containing pytest 9.1.1, pytest-asyncio, httpx, and ruff 0.16.3. Add:

~~~toml
[project.scripts]
broccoli-desktop = "broccoli_desktop.__main__:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.uv]
required-version = ">=0.11"
~~~

Create broccoli_desktop/__init__.py containing __version__ = "0.1.0" and a minimal __main__.py that imports main without starting runtime behavior during import.

- [ ] **Step 3: Add repository hygiene and frontend configuration**

Create .gitattributes with * text=auto eol=lf and binary declarations for ico, png, exe, and dll. Create .editorconfig matching the main repository’s UTF-8/LF/Python-4-space rules. Ignore .venv, __pycache__, .ruff_cache, .pytest_cache, .env, build, dist, *.spec, node_modules, artifacts/visual, and broccoli_desktop/static/output.css.

Create ui/package.json with Tailwind 4.1.17, @tailwindcss/cli 4.1.17, daisyui 5.5.8, and a build:css script that runs:

~~~json
"build:css": "npx tailwindcss -i app.css -o ../broccoli_desktop/static/output.css"
~~~

Create ui/app.css with @import "tailwindcss", @plugin "daisyui", light and dark Broccoli color themes, and @source "../broccoli_desktop/static".

- [ ] **Step 4: Add PowerShell development scripts and documentation**

Create scripts/sync.ps1 to run uv sync --frozen, scripts/check.ps1 to run Ruff format --check then Ruff check then pytest, and README.md documenting uv installation, npm ci in ui, the --local and --dev flags, and that the remote backend is an external prerequisite. Initialize CHANGELOG.md with an Unreleased section and a 0.1.0 Added entry for the desktop client scaffold.

- [ ] **Step 5: Provision and verify the local development environment**

Run:

~~~powershell
uv sync
Set-Location ui
npm ci
npm run build:css
Set-Location ..
.\.venv\Scripts\python.exe -m pytest tests/test_project.py -v
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check .
~~~

Expected: the metadata test passes, CSS is generated, and both Ruff commands pass.

- [ ] **Step 6: Commit the bootstrap**

~~~powershell
git add pyproject.toml uv.lock .python-version ruff.toml .editorconfig .gitattributes .gitignore CHANGELOG.md README.md broccoli_desktop tests ui scripts
git commit -m "chore: bootstrap Broccoli Desktop"
~~~

---

### Task 2: Implement configuration, credentials, and shared local models

**Files:**
- Create: broccoli_desktop/config.py, broccoli_desktop/models.py, broccoli_desktop/credentials.py
- Create: tests/test_config.py, tests/test_credentials.py
- Modify: broccoli_desktop/__main__.py

**Interfaces:**
- Consumes: the console entry point from Task 1 and keyring.
- Produces: RuntimeConfig, CredentialStore, SessionSummary, TranscriptSegment, DeviceDescriptor, and local request/response models used by all later tasks.

- [ ] **Step 1: Write failing configuration and credential tests**

~~~python
from broccoli_desktop.config import RuntimeConfig, parse_runtime_config
from broccoli_desktop.credentials import CredentialStore

def test_local_flag_overrides_the_embedded_production_url():
    config = parse_runtime_config(["--local"])
    assert config.server_url == "http://127.0.0.1:8000"
    assert config.environment == "local"

def test_deleting_a_token_uses_the_fixed_service_and_account(fake_keyring):
    store = CredentialStore(fake_keyring)
    store.delete_token()
    assert fake_keyring.deleted == [("Broccoli Desktop", "api-token")]
~~~

- [ ] **Step 2: Implement RuntimeConfig and CLI parsing**

Define:

~~~python
@dataclass(frozen=True)
class RuntimeConfig:
    environment: Literal["production", "development", "local"]
    server_url: str

def parse_runtime_config(argv: Sequence[str]) -> RuntimeConfig:
    ...
~~~

Use a production URL constant compiled into config.py, --local for http://127.0.0.1:8000, and --dev for the non-secret BROCCOLI_DESKTOP_DEV_URL environment variable. Reject using --local and --dev together with argparse’s mutually exclusive group. Make __main__.main pass parsed RuntimeConfig into runtime.start.

- [ ] **Step 3: Implement keyring isolation**

Create a KeyringProtocol with get_password, set_password, and delete_password. CredentialStore must always use service Broccoli Desktop and account api-token:

~~~python
class CredentialStore:
    def load_token(self) -> str | None: ...
    def save_token(self, token: str) -> None: ...
    def delete_token(self) -> None: ...
~~~

Strip pasted tokens, reject empty tokens before saving, and never include a token in repr, exception text, or local event payloads.

- [ ] **Step 4: Define immutable domain models**

Create dataclasses for DeviceDescriptor, SessionSummary, TranscriptSegment, SessionPage, ConnectionState, TranscriptDelta, and UiEvent. Ensure SessionSummary includes UUID code, title, status, started/ended values, device label, segment count, and is_live. Define title validation once with a 120-character maximum.

- [ ] **Step 5: Run targeted tests and lint**

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_credentials.py -v
.\.venv\Scripts\ruff.exe format broccoli_desktop tests
.\.venv\Scripts\ruff.exe check --fix broccoli_desktop tests
.\.venv\Scripts\ruff.exe check broccoli_desktop tests
~~~

Expected: configuration rejects conflicting flags, credential operations use fixed names, and no token appears in failures.

- [ ] **Step 6: Commit configuration and credentials**

~~~powershell
git add broccoli_desktop/config.py broccoli_desktop/models.py broccoli_desktop/credentials.py broccoli_desktop/__main__.py tests/test_config.py tests/test_credentials.py
git commit -m "feat: add desktop configuration and credential storage"
~~~

---

### Task 3: Implement the external Listening contract adapter and fakes

**Files:**
- Create: broccoli_desktop/remote.py, tests/fakes.py, tests/test_remote.py
- Modify: broccoli_desktop/models.py

**Interfaces:**
- Consumes: RuntimeConfig and shared models from Task 2.
- Produces: ListeningRemote protocol and HttpListeningRemote implementation used by SessionController.
- External dependency: the backend contract in the approved spec; this task does not alter or test C:\repos\broccoli.

- [ ] **Step 1: Write failing adapter tests against an in-process fake transport**

~~~python
@pytest.mark.asyncio
async def test_list_sessions_sends_token_header_and_maps_cursor_page(fake_transport):
    remote = HttpListeningRemote("https://broccoli.example", "secret", fake_transport)

    page = await remote.list_sessions(cursor="next", query="daily")

    assert fake_transport.requests[0].headers["Authorization"] == "Token secret"
    assert fake_transport.requests[0].url.endswith("/api/listening/desktop/sessions/?cursor=next&q=daily")
    assert page.next_cursor == "cursor-2"

@pytest.mark.asyncio
async def test_remote_delta_and_segment_share_the_utterance_identifier(fake_socket_factory):
    remote = HttpListeningRemote("https://broccoli.example", "secret", socket_factory=fake_socket_factory)

    stream = await remote.connect_stream(resume_code="session-1", device_label="Laptop")
    events = [event async for event in stream.events()]

    assert events[0].utterance_id == events[1].utterance_id
~~~

- [ ] **Step 2: Define the complete remote protocol**

Define a protocol that makes the external boundary explicit:

~~~python
class ListeningRemote(Protocol):
    async def verify_token(self) -> SessionPage: ...
    async def list_sessions(self, cursor: str | None, query: str) -> SessionPage: ...
    async def get_session(self, uuid_code: str) -> SessionSummary: ...
    async def list_segments(self, uuid_code: str, cursor: str | None) -> SegmentPage: ...
    async def update_title(self, uuid_code: str, title: str) -> SessionSummary: ...
    async def connect_stream(
        self, *, resume_code: str | None, device_label: str
    ) -> RemoteStream: ...

class RemoteStream(Protocol):
    async def send_bytes(self, frame: bytes) -> None: ...
    async def send_control(self, message: dict[str, str]) -> None: ...
    def events(self) -> AsyncIterator[RemoteEvent]: ...
    async def close(self) -> None: ...
~~~

RemoteStream sends bytes and control JSON and yields session.started, transcript.delta, transcript.segment, credit.warning, session.ended, error, and pong messages.

- [ ] **Step 3: Implement HTTP and WebSocket mapping**

Use httpx.AsyncClient for the four REST resources and websockets for the external WebSocket. Convert an HTTPS base URL to WSS and an HTTP base URL to WS. Send Authorization: Token <token> only from Python. Parse session.started as a typed event requiring uuid_code, next_seq, next_offset_ms, and max_duration_s. Reject unexpected event types with a RemoteProtocolError that contains event type only, never payload text.

- [ ] **Step 4: Implement deterministic fakes**

Create FakeListeningRemote with preloaded pages, segments, and a FakeRemoteStream that records frames/control messages and yields scripted events. Include helpers for unauthorized login, token revocation, a resume event with nonzero next_offset_ms, a delta/final pair, and a remote close. No fake may contact a network endpoint.

- [ ] **Step 5: Run adapter tests**

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_remote.py -v
.\.venv\Scripts\ruff.exe format broccoli_desktop/remote.py broccoli_desktop/models.py tests/fakes.py tests/test_remote.py
.\.venv\Scripts\ruff.exe check --fix broccoli_desktop/remote.py broccoli_desktop/models.py tests/fakes.py tests/test_remote.py
.\.venv\Scripts\ruff.exe check broccoli_desktop/remote.py broccoli_desktop/models.py tests/fakes.py tests/test_remote.py
~~~

Expected: all REST paths, token headers, URL conversion, offset parsing, and delta matching are asserted without remote access.

- [ ] **Step 6: Commit the external adapter**

~~~powershell
git add broccoli_desktop/remote.py broccoli_desktop/models.py tests/fakes.py tests/test_remote.py
git commit -m "feat: add external Listening contract adapter"
~~~

---

### Task 4: Implement binary protocol, VAD, resampling, and frame batching

**Files:**
- Create: broccoli_desktop/protocol.py, broccoli_desktop/audio.py
- Create: tests/test_protocol.py, tests/test_audio.py
- Modify: broccoli_desktop/models.py

**Interfaces:**
- Consumes: DeviceDescriptor and the external binary protocol specification.
- Produces: encode_audio_frame, decode_audio_frame, and AudioPipeline.feed(channel, pcm_48k) -> list[AudioFrame].

- [ ] **Step 1: Write failing protocol and pipeline tests**

~~~python
def test_encode_audio_frame_uses_the_listening_header():
    frame = encode_audio_frame(channel="mic", offset_ms=700, pcm=b"\x01\x00")

    assert frame[:2] == bytes([1, 0])
    assert int.from_bytes(frame[2:10], "big") == 700
    assert frame[10:] == b"\x01\x00"

def test_pipeline_emits_one_100ms_24khz_frame_after_five_speech_blocks(fake_vad, fake_resampler):
    pipeline = AudioPipeline(vad=fake_vad, resampler=fake_resampler)

    frames = [frame for _ in range(5) for frame in pipeline.feed("system", pcm_20ms())]

    assert len(frames) == 1
    assert frames[0].channel == "system"
    assert len(frames[0].pcm) == 4_800
~~~

- [ ] **Step 2: Implement the Listening binary codec**

Implement a version-1 encoder with channel mapping mic=0 and system=1:

~~~python
def encode_audio_frame(*, channel: Literal["mic", "system"], offset_ms: int, pcm: bytes) -> bytes:
    if offset_ms < 0:
        raise ValueError("offset_ms must be non-negative")
    return bytes([1, CHANNEL_BYTES[channel]]) + offset_ms.to_bytes(8, "big") + pcm
~~~

Add a decoder solely for tests and local diagnostic validation. Reject unknown protocol version, unknown channel byte, and an audio payload with an odd number of bytes.

- [ ] **Step 3: Implement the audio pipeline**

Configure WebRTC VAD for 48 kHz, 20 ms mono PCM16 blocks. Keep a monotonic sample clock per capture period, pass only speech blocks to soxr, and accumulate resampled samples per channel until exactly 100 ms of 24 kHz PCM exists. Return AudioFrame(channel, offset_ms, pcm) with offsets based on capture start plus the session base offset. Silence must yield no remote frame and consume no reconnect-buffer capacity.

- [ ] **Step 4: Add boundary and offset tests**

Add tests for five speech blocks, silence suppression, mixed mic/system buffers, a 4,800-byte output frame, carry-over samples, and a resumed base offset:

~~~python
def test_resumed_pipeline_offsets_frames_after_the_remote_base(fake_vad, fake_resampler):
    pipeline = AudioPipeline(vad=fake_vad, resampler=fake_resampler, base_offset_ms=12_345)

    frame = collect_first_frame(pipeline, "mic")

    assert frame.offset_ms >= 12_345
~~~

- [ ] **Step 5: Run focused tests**

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_protocol.py tests/test_audio.py -v
.\.venv\Scripts\ruff.exe format broccoli_desktop/protocol.py broccoli_desktop/audio.py tests/test_protocol.py tests/test_audio.py
.\.venv\Scripts\ruff.exe check --fix broccoli_desktop/protocol.py broccoli_desktop/audio.py tests/test_protocol.py tests/test_audio.py
.\.venv\Scripts\ruff.exe check broccoli_desktop/protocol.py broccoli_desktop/audio.py tests/test_protocol.py tests/test_audio.py
~~~

Expected: frame encoding, VAD gating, 48 kHz-to-24 kHz conversion, and resume offsets pass deterministically with fakes.

- [ ] **Step 6: Commit audio transport**

~~~powershell
git add broccoli_desktop/protocol.py broccoli_desktop/audio.py tests/test_protocol.py tests/test_audio.py
git commit -m "feat: add VAD-gated listening audio frames"
~~~

---

### Task 5: Implement selected-device WASAPI capture

**Files:**
- Create: broccoli_desktop/capture.py, tests/test_capture.py
- Modify: broccoli_desktop/models.py, tests/fakes.py

**Interfaces:**
- Consumes: AudioPipeline from Task 4.
- Produces: CaptureBackend, PyAudioCaptureBackend, and CaptureSession.start(callback)/stop().
- Does not consume remote services or file storage.

- [ ] **Step 1: Write failing device and lifecycle tests**

~~~python
def test_enumeration_exposes_microphones_and_loopback_outputs(fake_pyaudio):
    backend = PyAudioCaptureBackend(fake_pyaudio)

    devices = backend.list_devices()

    assert [device.kind for device in devices] == ["mic", "system"]

def test_capture_session_stops_both_sources_when_one_callback_fails(fake_capture_backend):
    def failing_callback(_: bytes) -> None:
        raise RuntimeError("send failed")

    session = CaptureSession(fake_capture_backend, "mic-1", "system-1", failing_callback)

    with pytest.raises(RuntimeError):
        session.start()

    assert fake_capture_backend.closed_sources == {"mic-1", "system-1"}
~~~

- [ ] **Step 2: Define the capture boundary**

Implement:

~~~python
class CaptureBackend(Protocol):
    def list_devices(self) -> list[DeviceDescriptor]: ...
    def open_microphone(self, device_id: str, on_pcm: Callable[[bytes], None]) -> CaptureHandle: ...
    def open_loopback(self, device_id: str, on_pcm: Callable[[bytes], None]) -> CaptureHandle: ...

class CaptureSession:
    def start(self) -> None: ...
    def stop(self) -> None: ...
~~~

DeviceDescriptor.kind is mic or system. Stable device IDs, not display names, are saved in local settings. Never save audio samples.

- [ ] **Step 3: Implement the PyAudioWPatch adapter**

Use PyAudioWPatch to enumerate input-capable devices and WASAPI output devices. Open the selected microphone directly and the selected output through its loopback input. Configure both streams to emit 20 ms 48 kHz mono PCM16 blocks. Move callback work onto bounded in-process queues so a capture callback never waits for network I/O.

- [ ] **Step 4: Implement restart-safe cleanup**

CaptureSession starts both sources atomically from the caller’s perspective. If the second source cannot start, close the first and report a DeviceUnavailableError containing the selected display name. stop is idempotent, drains queues, and closes both handles. A removed device publishes a local device_lost event and stops capture; SessionController may resume only after the user selects a replacement.

- [ ] **Step 5: Run capture tests**

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_capture.py -v
.\.venv\Scripts\ruff.exe format broccoli_desktop/capture.py tests/test_capture.py tests/fakes.py
.\.venv\Scripts\ruff.exe check --fix broccoli_desktop/capture.py tests/test_capture.py tests/fakes.py
.\.venv\Scripts\ruff.exe check broccoli_desktop/capture.py tests/test_capture.py tests/fakes.py
~~~

Expected: no physical device is required, both sources are closed in every failure path, and device IDs remain stable.

- [ ] **Step 6: Commit capture support**

~~~powershell
git add broccoli_desktop/capture.py broccoli_desktop/models.py tests/test_capture.py tests/fakes.py
git commit -m "feat: add selected WASAPI capture sources"
~~~

---

### Task 6: Implement session lifecycle, delta replacement, and reconnect recovery

**Files:**
- Create: broccoli_desktop/events.py, broccoli_desktop/session.py, tests/test_session.py
- Modify: tests/fakes.py

**Interfaces:**
- Consumes: ListeningRemote, CaptureSession, AudioPipeline, and CredentialStore-independent shared models.
- Produces: DesktopSessionController and EventHub, consumed by the local FastAPI API and tray.
- External dependency: SessionSummary, next_offset_ms, delta, segment, and termination events are supplied by the external contract.

- [ ] **Step 1: Write failing lifecycle tests**

~~~python
@pytest.mark.asyncio
async def test_resume_uses_remote_offset_and_replaces_the_pending_delta(fake_remote, fake_capture):
    controller = DesktopSessionController(fake_remote, fake_capture)
    await controller.resume("session-1", CaptureChoices("mic-1", "system-1"))

    await fake_remote.emit_delta("system", "utterance-1", "we should")
    await fake_remote.emit_segment("system", "utterance-1", "we should ship", 500, 1_000)

    events = controller.events.snapshot()
    assert events[-1].type == "segment"
    assert "utterance-1" not in controller.pending_deltas

@pytest.mark.asyncio
async def test_reconnect_discards_audio_beyond_ten_seconds(fake_clock, fake_remote, fake_capture):
    controller = DesktopSessionController(fake_remote, fake_capture, clock=fake_clock)
    await controller.start_new(CaptureChoices("mic-1", "system-1"), title="Daily")

    controller.enqueue_audio_frames(make_frames(milliseconds=11_000))

    assert controller.buffered_audio_ms <= 10_000
~~~

- [ ] **Step 2: Implement local events and state**

Define ConnectionState as idle, starting, streaming, reconnecting, stopped, failed, and device_selection_required. EventHub supports subscribe, unsubscribe, publish, and snapshot; publish immutable UiEvent values only. Events include session metadata, status, delta, segment, warning, and recoverable error. Do not include raw PCM or the token.

- [ ] **Step 3: Implement start, resume, stop, and title flows**

DesktopSessionController must expose:

~~~python
async def start_new(self, choices: CaptureChoices, title: str) -> SessionSummary: ...
async def resume(self, uuid_code: str, choices: CaptureChoices) -> SessionSummary: ...
async def stop(self) -> None: ...
async def update_title(self, uuid_code: str, title: str) -> SessionSummary: ...
~~~

start_new opens the remote stream without a resume code. resume opens it with the selected UUID and initializes AudioPipeline with session.started.next_offset_ms. stop closes capture first, sends session.end, flushes no local transcript state to disk, and publishes stopped.

- [ ] **Step 4: Implement bounded recovery**

On a recoverable stream failure, transition to reconnecting, keep at most 10,000 ms of already encoded VAD-gated frames, then retry with capped exponential delays of 1, 2, 4, 8, and 15 seconds. Reopen with the current session UUID, reset the pipeline base offset from the new session.started event, send buffered frames in offset order, and return to streaming. If device capture stopped, token fails, credit is denied, or session.end is received, stop retries and publish the corresponding terminal state.

- [ ] **Step 5: Run lifecycle tests**

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_session.py -v
.\.venv\Scripts\ruff.exe format broccoli_desktop/events.py broccoli_desktop/session.py tests/test_session.py tests/fakes.py
.\.venv\Scripts\ruff.exe check --fix broccoli_desktop/events.py broccoli_desktop/session.py tests/test_session.py tests/fakes.py
.\.venv\Scripts\ruff.exe check broccoli_desktop/events.py broccoli_desktop/session.py tests/test_session.py tests/fakes.py
~~~

Expected: sessions start and resume with correct offsets, deltas disappear after finals, reconnect buffers remain bounded, and terminal events stop retries.

- [ ] **Step 6: Commit session control**

~~~powershell
git add broccoli_desktop/events.py broccoli_desktop/session.py tests/test_session.py tests/fakes.py
git commit -m "feat: add desktop session recovery and transcript events"
~~~

---

### Task 7: Expose the loopback FastAPI API and local event socket

**Files:**
- Create: broccoli_desktop/api.py, tests/test_api.py
- Modify: broccoli_desktop/session.py

**Interfaces:**
- Consumes: CredentialStore, ListeningRemote factory, DesktopSessionController, EventHub, and CaptureBackend.
- Produces: create_app(services) -> FastAPI and a loopback-only JSON API for the static UI.

- [ ] **Step 1: Write failing API tests**

~~~python
def test_login_verifies_before_storing_the_token(client, fake_remote_factory, fake_credentials):
    response = client.post("/api/login", json={"token": "candidate"})

    assert response.status_code == 204
    assert fake_remote_factory.created_tokens == ["candidate"]
    assert fake_credentials.saved_tokens == ["candidate"]

def test_login_does_not_store_a_rejected_token(client, rejecting_remote_factory, fake_credentials):
    response = client.post("/api/login", json={"token": "bad"})

    assert response.status_code == 401
    assert fake_credentials.saved_tokens == []
~~~

- [ ] **Step 2: Define request and response routes**

Implement only loopback endpoints:

~~~text
POST   /api/login
DELETE /api/login
GET    /api/bootstrap
GET    /api/devices
GET    /api/sessions?cursor=&q=
GET    /api/sessions/{uuid_code}
GET    /api/sessions/{uuid_code}/segments?cursor=
PATCH  /api/sessions/{uuid_code}
POST   /api/sessions
POST   /api/sessions/{uuid_code}/resume
POST   /api/sessions/stop
WS     /api/events
GET    /
~~~

POST /api/login verifies the candidate token by calling remote.verify_token before CredentialStore.save_token. GET /api/bootstrap returns only authenticated boolean, selected-device IDs, current local state, and the first session page; it never returns token material.

- [ ] **Step 3: Implement dependency injection and authorization mapping**

Create a Services dataclass that contains protocols and factories, allowing TestClient to inject fakes. Return 401 for invalid/revoked remote tokens after deleting the stored credential, 409 for an invalid local state transition, 422 for invalid title/device input, and 503 for a recoverable remote outage. Bind Uvicorn to 127.0.0.1 and reject any Host header other than localhost, 127.0.0.1, or the assigned loopback port.

- [ ] **Step 4: Implement the event WebSocket**

Connect each browser event socket to EventHub. Send a bootstrap event on connection, then serialized UiEvent values. On disconnect, unsubscribe without affecting capture. The UI socket is read-only; ignore any incoming browser message and never proxy arbitrary remote WebSocket messages.

- [ ] **Step 5: Run FastAPI tests**

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py -v
.\.venv\Scripts\ruff.exe format broccoli_desktop/api.py tests/test_api.py
.\.venv\Scripts\ruff.exe check --fix broccoli_desktop/api.py tests/test_api.py
.\.venv\Scripts\ruff.exe check broccoli_desktop/api.py tests/test_api.py
~~~

Expected: token verification precedes persistence, all browser routes use injected services, events are one-way, and no response contains the submitted token.

- [ ] **Step 6: Commit local API**

~~~powershell
git add broccoli_desktop/api.py broccoli_desktop/session.py tests/test_api.py
git commit -m "feat: expose the local desktop API"
~~~

---

### Task 8: Build the Caderno de reunião UI with Tailwind and daisyUI

**Files:**
- Create: broccoli_desktop/static/index.html, broccoli_desktop/static/app.js
- Modify: ui/app.css, ui/package.json, tests/fakes.py, tests/test_api.py
- Generated: broccoli_desktop/static/output.css

**Interfaces:**
- Consumes: every /api route and the read-only /api/events socket from Task 7.
- Produces: token login, historical session library, device selectors, capture controls, session-code copy/open actions, and transcript rendering.

- [ ] **Step 1: Add static-shell assertions**

~~~python
def test_root_serves_the_desktop_shell(client):
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="loginView"' in response.text
    assert 'id="sessionLibrary"' in response.text
    assert 'id="transcriptTimeline"' in response.text
    assert "output.css" in response.text
~~~

- [ ] **Step 2: Create the accessible daisyUI shell**

Build index.html with three stateful regions: loginView, mainView, and persistent statusBanner. mainView contains an aside with search input, New session button, Load more button, and sessionLibrary; it contains a main element with editable title, mic/system selectors, start/resume/stop controls, record notice, copy code button, open Broccoli button, and transcriptTimeline. Use literal daisyUI classes only; do not construct class names at runtime.

Give every interaction a visible accessible name and a stable `data-testid` so the visual test can use semantic locators without brittle CSS selectors: `token-input`, `login-submit`, `login-error`, `session-search`, `new-session`, `load-more`, `session-library`, `session-row-<uuid>`, `resume-session`, `stop-session`, `copy-session-code`, `open-broccoli`, `status-banner`, and `transcript-timeline`. The external-open control must expose its target URL as an `href`; the visual test reads this attribute and never opens a remote page.

- [ ] **Step 3: Implement the local UI client**

In app.js, create a local fetch helper that calls only relative /api URLs. Implement login, logout, paginated search, title patch, new session, resume, stop, device refresh, clipboard copy, and an `open-broccoli` anchor whose Bootstrap-supplied official URL is its `href`. Keep token text only inside the submit handler; clear the input before rendering any next state.

Maintain Map keyed by utterance_id for pending deltas. Render delta text in an italic channel-specific row; replace it when segment arrives. Track whether timeline.scrollTop is near its bottom before auto-scrolling after a transcript event.

- [ ] **Step 4: Style the approved layout and operational states**

Use ui/app.css to define the Broccoli light/dark daisyUI themes and source the static directory. Style session state badges, channel labels, reconnecting banner, persistent recording notice, empty history, device-required state, and error actions. Ensure every displayed class occurs literally in static files so Tailwind emits it.

- [ ] **Step 5: Build CSS and test the shell**

~~~powershell
Set-Location ui
npm ci
npm run build:css
Set-Location ..
.\.venv\Scripts\python.exe -m pytest tests/test_api.py::test_root_serves_the_desktop_shell -v
~~~

Expected: output.css is created, the app shell is served locally, and its required DOM hooks exist.

- [ ] **Step 6: Define the visual-test contract for the fake UI server**

Extend the deterministic fakes so the later browser-only test server can exercise these UI-visible states without a real remote endpoint or audio device: rejected `bad-token` login; accepted `visual-test-token` login; two history pages containing `Daily` and `Planning`; search filtering; a resumed `Daily` session that emits a delta followed by a final segment with the same `utterance_id`; a recoverable close that exposes reconnecting before streaming; and a captured-device-loss event. The fake bootstrap supplies `http://127.0.0.1:8000` as its official Broccoli URL. The fake-only runner is implemented in Task 9; do not add a debug endpoint to the production local API.

Update `tests/test_api.py` to assert the required test IDs and accessible labels exist in the served shell. This locks the contract used by `scripts/visual-check.ps1` before the browser automation is added.

- [ ] **Step 7: Commit the UI**

~~~powershell
git add broccoli_desktop/static/index.html broccoli_desktop/static/app.js ui/app.css ui/package.json ui/package-lock.json tests/fakes.py tests/test_api.py
git commit -m "feat: add Broccoli Desktop meeting notebook UI"
~~~

---

### Task 9: Integrate PyWebView and the Windows tray lifecycle

**Files:**
- Create: broccoli_desktop/runtime.py, broccoli_desktop/tray.py, tests/test_runtime.py, tests/test_tray.py, tests/visual_server.py
- Create: scripts/run.ps1, scripts/visual-check.ps1
- Modify: broccoli_desktop/__main__.py

**Interfaces:**
- Consumes: FastAPI application factory, DesktopSessionController, and EventHub.
- Produces: start_runtime(config) and TrayController with show, stop, and quit behavior.

- [ ] **Step 1: Write failing runtime and tray tests**

~~~python
def test_window_close_hides_instead_of_stopping_capture(fake_window, fake_tray, runtime):
    runtime.on_window_closing()

    assert fake_window.hidden is True
    assert fake_tray.running is True
    assert runtime.session.stop_calls == 0

def test_quit_requires_confirmation_when_capture_is_active(fake_dialog, runtime):
    runtime.session.state = ConnectionState.STREAMING
    fake_dialog.answer = False

    runtime.request_quit()

    assert runtime.window.destroyed is False
~~~

- [ ] **Step 2: Implement loopback runtime startup and the fake browser-only runner**

Start Uvicorn on an available 127.0.0.1 port in a managed thread, wait for its health endpoint, and create a PyWebView window pointed at that exact local URL. Pass no remote credentials into JavaScript bindings. If the server cannot become healthy, show a native error and exit without opening capture.

Create `tests/visual_server.py` as a test-only module that builds `create_app(Services(...))` from the deterministic fakes defined in Tasks 3, 5, and 6, then runs Uvicorn at an explicit `127.0.0.1` port. It must seed only `visual-test-token` and fake transcript/session data.

Create `scripts/run.ps1` with `-FakeRemote`, `-BrowserOnly`, and `-Port` parameters. The `-FakeRemote -BrowserOnly` combination starts `tests.visual_server` and does not create a PyWebView window; the default starts the desktop runtime. Its browser-only form is the only web address that `agent-browser` may open during automated visual QA:

~~~powershell
.\scripts\run.ps1 -FakeRemote -BrowserOnly -Port 8765
~~~

- [ ] **Step 3: Implement tray behavior**

Create a pystray menu with Show Broccoli Desktop, connection-status text, Stop capture, and Quit. Show restores and focuses the PyWebView window. Stop calls the same SessionController.stop path as the local API. The close-window callback hides the window. Quit asks through the runtime dialog when streaming, stops capture only after confirmation, shuts down Uvicorn, then destroys the window and tray.

- [ ] **Step 4: Add failure tests**

Add tests for server-start failure, tray stop action, a non-streaming quit without confirmation, and idempotent runtime shutdown. Use fake webview, Uvicorn runner, tray, and dialog implementations; no test imports the real GUI engine.

- [ ] **Step 5: Run runtime tests**

~~~powershell
.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_tray.py -v
.\.venv\Scripts\ruff.exe format broccoli_desktop/runtime.py broccoli_desktop/tray.py broccoli_desktop/__main__.py tests/test_runtime.py tests/test_tray.py tests/visual_server.py
.\.venv\Scripts\ruff.exe check --fix broccoli_desktop/runtime.py broccoli_desktop/tray.py broccoli_desktop/__main__.py tests/test_runtime.py tests/test_tray.py tests/visual_server.py
.\.venv\Scripts\ruff.exe check broccoli_desktop/runtime.py broccoli_desktop/tray.py broccoli_desktop/__main__.py tests/test_runtime.py tests/test_tray.py tests/visual_server.py
~~~

Expected: closing hides, tray actions reuse controller behavior, and shutdown cannot leave a loopback server or active capture thread behind.

- [ ] **Step 6: Run the agent-browser visual acceptance pass**

Implement `scripts/visual-check.ps1` to start the fake browser-only server in a hidden child process, wait for `http://127.0.0.1:8765/`, run one isolated agent-browser session, and always stop both the browser and server in `finally`. It must first verify `agent-browser --version` returns `agent-browser 0.34.0`, use `agent-browser session id --scope worktree --prefix broccoli-desktop-visual`, then pass that value through `--session` to every command. Do not use `--restore`, `--profile`, `network har`, a real credential, or a URL outside `127.0.0.1`.

Use a fixed desktop viewport and the snapshot/action/re-snapshot loop. The script must save only fake-data screenshots to the ignored `artifacts/visual/` directory, fail on a missing expected state, and run an axe audit. Its core commands are:

~~~powershell
$session = agent-browser session id --scope worktree --prefix broccoli-desktop-visual
$browser = @("--session", $session, "--allowed-domains", "127.0.0.1,localhost")
agent-browser @browser open http://127.0.0.1:8765/
agent-browser @browser set viewport 1440 900
agent-browser @browser set media light
agent-browser @browser wait --text "Entrar"
agent-browser @browser snapshot -i
agent-browser @browser screenshot --full artifacts/visual/login.png
agent-browser @browser find testid token-input fill bad-token
agent-browser @browser find testid login-submit click
agent-browser @browser wait --text "Token inválido"
agent-browser @browser snapshot -i
agent-browser @browser find testid token-input fill visual-test-token
agent-browser @browser find testid login-submit click
agent-browser @browser wait --text "Nova sessão"
agent-browser @browser snapshot -i
agent-browser @browser screenshot --full artifacts/visual/notebook-light.png
agent-browser @browser find testid session-search fill Daily
agent-browser @browser wait --text "Daily"
agent-browser @browser find testid session-row-session-1 click
agent-browser @browser snapshot -i
agent-browser @browser find testid resume-session click
agent-browser @browser wait --text "we should ship"
agent-browser @browser find testid copy-session-code click
agent-browser @browser wait --text "Código copiado"
agent-browser @browser get attr '[data-testid="open-broccoli"]' href
agent-browser @browser wait --text "Transmitindo"
agent-browser @browser screenshot --full artifacts/visual/notebook-streaming.png
agent-browser @browser set media dark
agent-browser @browser reload
agent-browser @browser wait --text "Caderno de reunião"
agent-browser @browser screenshot --full artifacts/visual/notebook-dark.png
agent-browser @browser a11y --tags wcag2a,wcag2aa
agent-browser @browser eval "[document.documentElement.innerText.includes('visual-test-token'), localStorage.length, sessionStorage.length]"
~~~

Interpret the final `eval` result as `[false, 0, 0]`; fail the script if it differs. Capture the `get attr` result and fail unless it equals the fake bootstrap URL `http://127.0.0.1:8000`. Run `agent-browser @browser errors --json` and fail if its JSON array is nonempty; run `agent-browser @browser a11y --tags wcag2a,wcag2aa --json` and fail if its `violations` array is nonempty. Inspect `login.png`, `notebook-light.png`, `notebook-streaming.png`, and `notebook-dark.png` with the image viewer before accepting the task: login must be legible, the meeting-notebook sidebar and transcript must remain visible at 1440x900, status/recording indicators must be visually distinct, and dark mode must retain readable contrast. The test verifies layout and browser behavior; the Python suite remains responsible for state transitions that only a fake can trigger.

- [ ] **Step 7: Commit desktop runtime integration and visual QA**

~~~powershell
git add broccoli_desktop/runtime.py broccoli_desktop/tray.py broccoli_desktop/__main__.py scripts/run.ps1 scripts/visual-check.ps1 tests/test_runtime.py tests/test_tray.py tests/visual_server.py
git commit -m "feat: add desktop window and visual QA"
~~~

---

### Task 10: Package, install, and automate Broccoli Desktop

**Files:**
- Create: installer/BroccoliDesktop.spec, installer/BroccoliDesktop.iss
- Create: scripts/build-css.ps1, scripts/package.ps1, scripts/installer.ps1
- Create: .github/workflows/ci.yml
- Modify: README.md, CHANGELOG.md, .gitignore

**Interfaces:**
- Consumes: a passing Python suite, generated CSS, and the Windows-only package layout.
- Produces: a PyInstaller onedir bundle, an Inno Setup installer, and Windows CI artifacts.

- [ ] **Step 1: Write failing packaging-configuration tests**

~~~python
from pathlib import Path

def test_pyinstaller_spec_collects_static_assets_and_audio_dependencies():
    spec = Path("installer/BroccoliDesktop.spec").read_text(encoding="utf-8")

    assert "broccoli_desktop/static" in spec
    assert "pyaudiowpatch" in spec
    assert "webview" in spec

def test_installer_declares_windows_11_x64_and_webview2_check():
    installer = Path("installer/BroccoliDesktop.iss").read_text(encoding="utf-8")

    assert "ArchitecturesAllowed=x64compatible" in installer
    assert "WebView2" in installer
~~~

- [ ] **Step 2: Implement reproducible PowerShell build scripts**

build-css.ps1 runs npm ci then npm run build:css from ui. package.ps1 runs build-css.ps1 then PyInstaller against installer/BroccoliDesktop.spec. installer.ps1 runs package.ps1 then invokes ISCC with installer/BroccoliDesktop.iss. Every script exits on error and clears only its own explicit build/dist path.

- [ ] **Step 3: Create the PyInstaller and Inno Setup specifications**

The PyInstaller spec must bundle broccoli_desktop/static, the application icon, pywebview runtime modules, PyAudioWPatch, soxr, keyring dependencies, and pystray dependencies. Build onedir, not onefile.

The Inno Setup script installs only the generated onedir tree, creates Start Menu and desktop shortcuts named Broccoli Desktop, creates an uninstaller, and checks that WebView2 is present before launching. Do not add an updater, telemetry, or administrator requirement.

- [ ] **Step 4: Add Windows CI, including visual browser checks**

Create a GitHub Actions workflow on windows-latest that installs Python 3.12 and uv, runs uv sync --frozen, npm ci, CSS build, scripts/check.ps1, scripts/package.ps1, and scripts/installer.ps1, then uploads the installer as an artifact. Before the visual job, run `npm install --global agent-browser@0.34.0`, `agent-browser install`, and `scripts/visual-check.ps1`; publish `artifacts/visual/` only when that step fails. CI does not use a real credential, microphone, remote Broccoli endpoint, profile, restore state, or HAR capture.

- [ ] **Step 5: Run packaging checks and the full suite**

~~~powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check .
.\scripts\build-css.ps1
.\scripts\package.ps1
.\scripts\installer.ps1
.\scripts\visual-check.ps1
~~~

Expected: all tests, lint, and visual checks pass; `dist` contains a runnable onedir bundle; installer output contains a Broccoli Desktop .exe; and the visual artifacts show only fake data.

- [ ] **Step 6: Perform the Windows acceptance pass**

On a clean Windows 11 x64 account, install the generated .exe and verify:

1. Login stores a valid token in Credential Manager and logout removes it.
2. The session library, title edit, transcript pagination, and historical resume work against a backend environment whose owner confirms the external prerequisites.
3. Selected microphone and output loopback show separate transcript attribution.
4. Text partials are replaced by confirmed segments.
5. Closing hides to tray; Stop and Quit behave as specified.
6. Network retry never retains more than ten seconds of audio and never writes an audio file.
7. A revoked token returns to login and no token remains in local UI storage.
8. The agent-browser fake-server pass has screenshots for login, notebook light/dark modes, and streaming; its axe audit has no violations.

Record the environment URL and backend-contract confirmation outside the repository; do not record credentials or transcript content.

- [ ] **Step 7: Update release documentation and commit**

Update README.md with installer instructions and CHANGELOG.md under 0.1.0 Added with capture, library, tray, and packaging. Then run:

~~~powershell
git add installer scripts .github README.md CHANGELOG.md .gitignore tests/test_project.py
git commit -m "build: package Broccoli Desktop for Windows"
~~~

## Plan self-review

- **Spec coverage:** Tasks 1–2 cover repository conventions, fixed dependencies, runtime URLs, and token storage. Tasks 3 and 6 cover the external prerequisite contract without changing it. Tasks 4–5 cover separate WASAPI sources, VAD, resampling, offsets, and no disk audio. Tasks 7–9 cover the loopback API, Caderno de reunião UI, deltas, tray, recovery, and agent-browser visual QA. Task 10 covers installer, CI, manual acceptance, and the external release gate.
- **No-backend scope:** No task names a file, command, migration, test, or deployment under C:\repos\broccoli. External endpoints are exercised only via fakes and a separately confirmed environment.
- **Placeholder scan:** This document contains no deferred implementation markers; every task supplies exact target files, interfaces, commands, test cases, and commit contents.
- **Type consistency:** ListeningRemote is defined before DesktopSessionController consumes it; AudioPipeline precedes CaptureSession and controller use; EventHub precedes API and tray use; create_app precedes runtime startup.
