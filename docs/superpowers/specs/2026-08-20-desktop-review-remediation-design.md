# Broccoli Desktop — Review Remediation Design

**Date:** 2026-08-20
**Status:** Approved for planning
**Baseline:** `5295953` on `fix/review-remediation`

## Purpose

Close every finding from the 2026-08-20 code and usability review of this
repository: 3 Critical, 16 Important, 15 Minor. Two of the Criticals block any
release — the local API has no origin protection, and the product cannot be
packaged from a clean checkout.

This document covers `broccoli-app` only. The platform's findings are specified
separately in the `broccoli` repository under the same date.

## Decisions taken before this design

These were open questions in the review. They are settled here so the plan does
not have to reopen them.

| Question | Decision |
|---|---|
| Production hostname pinned in `config.py` vs. README forbidding it | **Keep it embedded.** The design spec is right; the README sentence is wrong and gets rewritten to be about tokens and transcripts only. |
| The dead proxy settings panel | **Implement it for real.** Config through settings, password through `CredentialStore`, injected into `httpx` and `websockets`. |
| The removed "open Broccoli web" action | **Accept the removal.** Update the design spec; copying the code stays the whole flow. |
| Stale VAD / capabilities specs | **Update the specs** to describe what the code does today. |

## Guiding constraints

- **No behaviour change without a test that would have caught the bug.** Three
  of this review's findings survived because no test exercised the failure
  (foreign `Origin`, a slow socket, a device disappearing mid-stream). Each
  phase names the test that closes that gap.
- **Keep changes focused.** No refactor beyond what a finding requires.
- **The fakes stay fakes.** `tests/fakes.py` is good; the gap is which scenarios
  are scripted, not the fidelity of the doubles.

---

## Phase 1 — Trust boundary of the local API

**Closes:** C1.

### The problem

`LoopbackHostMiddleware` (`api.py:194-233`) validates only the `Host` header.
WebSockets are not subject to the same-origin policy, so a page on any origin
can open `ws://127.0.0.1:PORT/api/events`, and the browser sends
`Host: 127.0.0.1:PORT`, which the middleware accepts. Verified in a browser: a
page served from `http://127.0.0.1:9999` received the bootstrap — authentication
state, device labels, session title, `is_live` — and would have received every
`delta` and `segment` after it.

The ephemeral port is not a defence: the same `open` event that leaks the
transcript also reveals which port is live.

### Design

Two independent layers, because they stop different attackers.

**Layer 1 — Origin validation.** `LoopbackHostMiddleware` gains an `Origin`
check alongside its `Host` check:

- If the request carries no `Origin`, it passes. Non-browser local callers
  (the app's own `httpx`, a developer's `curl`) send none.
- If it carries one, it must be exactly `http://127.0.0.1:{port}` or
  `http://localhost:{port}` for the port this server is bound to. Anything else
  is rejected: HTTP with `403`, WebSocket with close code `1008`.
- Browsers always send `Origin` on a WebSocket handshake, so this alone closes
  the reproduced attack.

**Layer 2 — Per-launch capability token.** `Origin` does not stop another
*process* on the machine, which sends no `Origin` at all.

- `Services` gains `capability_token: str | None = None`.
- `runtime.py` generates `secrets.token_urlsafe(32)` at startup, passes it into
  `Services`, and appends it to the window URL as `?k=<token>`.
- `app.js` reads the token from `location.search` on load, keeps it in a module
  constant, and immediately strips it from the address bar with
  `history.replaceState` so it does not linger in a visible URL.
- Every `/api/*` request carries it in an `X-Broccoli-Key` header. The
  WebSocket, which cannot set headers, carries it as a query parameter — this
  URL never leaves the machine, so the review's objection to secrets in query
  strings (which was about the *remote* backend's access logs) does not apply.
- The middleware requires the token on `/api/*` only. `/` and `/static/*` are
  the shell and its assets, carry no data, and stay open so the page can boot.
- When `capability_token` is `None` the check is skipped. This exists so
  `tests/visual_server.py` can run with a fixed known value; a test asserts the
  production runtime path never produces `None`.

### Also in this phase

- `POST /api/login` returns `409` while a capture is active, instead of
  replacing `self.controller` and orphaning two open WASAPI streams and a remote
  WebSocket with no reference that can stop them (I6, `api.py:99-105`).
- `create_uvicorn_config` sets an explicit `timeout_graceful_shutdown`; the
  default waits indefinitely and the SSE stream never ends on its own
  (`api.py:236`).

### Tests

- A page-like request to `/api/events` with `Origin: http://evil.example`
  closes with `1008`; with the correct origin it opens. **This is the test whose
  absence let C1 through.**
- HTTP `/api/devices` with a foreign `Origin` gets `403`; with none, `200`.
- `/api/*` without `X-Broccoli-Key` gets `403`; `/` and `/static/app.js` do not.
- `runtime` startup produces a non-`None` `capability_token`.
- `POST /api/login` during an active capture returns `409` and the original
  controller is still the one holding the capture.

---

## Phase 2 — Audio runtime core

**Closes:** C3, I1, I2, I3.

### The problem

`session.py:449-453` turns every audio frame into a fire-and-forget task:

```python
loop.call_soon_threadsafe(lambda: asyncio.create_task(self._forward_frame(frame)))
```

CPython holds only a weak reference to a running task, so a frame can vanish
mid-flight under GC pressure — silent, intermittent audio loss. And nothing
bounds it: capture produces 20 frames/second/channel in real time, so a stalled
socket accumulates tasks without limit, each pinning ~4.8 KB of PCM.
`MAX_BUFFERED_AUDIO_MS` only applies once the state is already `RECONNECTING`;
a socket that is merely *slow* never gets there. Independent tasks can also
interleave at `send()`'s drain point and leave the socket out of offset order.

### Design

**One bounded queue, one sender task.**

- `DesktopSessionController` gains an `asyncio.Queue(maxsize=AUDIO_QUEUE_MAX_FRAMES)`
  where `AUDIO_QUEUE_MAX_FRAMES = 100` — 10 seconds at 100 ms frames, matching
  the existing reconnect bound so the two limits agree.
- The capture thread does `loop.call_soon_threadsafe(self._enqueue_frame, frame)`.
  `_enqueue_frame` calls `queue.put_nowait`; on `QueueFull` it discards the
  oldest frame, increments a dropped counter, and publishes a `UiEvent` so the
  interface can say audio is being dropped rather than failing silently.
- A single long-lived `_sender_task` drains the queue and awaits
  `stream.send(...)`. One consumer means offsets leave in the order they were
  produced, by construction.
- The controller keeps strong references to `_sender_task`, `_reader_task`, and
  any `_handle_device_loss` / `_release_remote` task in a `set`, each with
  `add_done_callback(self._tasks.discard)`. `stop()` cancels and awaits them,
  and clears `_reader_task` (which today is cancelled but never awaited or
  cleared, `session.py:129`).

**Blocking I/O off the event loop.** Wrap in `asyncio.to_thread`:
`credentials.load_token()`, `PyAudio.list_devices()`, `PyAudio.open()`, and
`stop_audio_level_test()` / `_QueuedCaptureHandle.close()`. Add a device-list
cache with a 5-second TTL so `/api/devices`, the bootstrap, and session start
stop re-enumerating on every call.

**Bound the event log.** `EventHub._events` becomes `deque(maxlen=256)`. It is
kept rather than deleted because `snapshot()` has one consumer (a test) and a
bounded deque preserves that at no cost, while ending the unbounded retention of
every transcript delta for the life of the process.

**Resume offset base.** Before starting the pipeline for a session resumed from
the library, read the last stored segment and use its `ended_offset_ms` as the
base, so new speech does not overwrite offsets 0…N that already hold the earlier
part of the meeting. The existing `GET /api/sessions/{uuid}/segments` paginates
ascending with an offset cursor, and the session payload already carries
`segment_count`, so the client jumps straight to the final page rather than
walking every page.

The backend validates the cursor with `_cursor_offset`, which rejects any offset
that is not a multiple of the page size (`views.py:61`). So the jump must land on
a page boundary:

```
cursor = ((segment_count - 1) // SEGMENT_PAGE_SIZE) * SEGMENT_PAGE_SIZE
```

for `segment_count > 0`, and no request at all when it is zero. Taking
`max(0, segment_count - page_size)` instead would send `150` for a 250-segment
session and be rejected with `Invalid cursor`. No backend change is required.
`_pipeline_base_offset_ms` is initialised in `__init__` rather than only in
`_set_pipeline` (`session.py:511`).

### Tests

- A remote whose `send` blocks indefinitely: the queue reaches its bound,
  frames are dropped oldest-first, a `UiEvent` reports it, and memory stops
  growing. **This is the slow-socket test whose absence let C3 through.**
- Frames arrive at the remote in offset order under concurrent enqueue.
- `stop()` leaves no task alive and no reference held.
- A device disappearing *during* streaming reaches `DEVICE_SELECTION_REQUIRED`
  through the real `PyAudioCaptureBackend` status-flag path, not only through
  `FakePyAudioStream.emit`.
- Resuming a session with existing segments starts at a base past the last
  stored `ended_offset_ms`.
- `EventHub` stops growing past its bound.

---

## Phase 3 — Build integrity

**Closes:** C2, I7.

### The problem

`installer/BroccoliDesktop.spec:10,52` sets `hookspath` to `installer/hooks/`,
whose only file was deleted in `0a93763` when VAD was dropped. Nothing under
that directory is tracked, and PyInstaller raises `FileNotFoundError` on a
missing hook directory. `package.ps1`, `installer.ps1`, and the `package` CI job
all fail from a clean checkout, which also blocks the `visual` job that needs
it. It works locally only because a stale `__pycache__` keeps the directory
alive.

### Design

- Delete the `HOOKS_DIRECTORY` constant and the `hookspath` argument.
- Delete `tests/test_project.py` entirely and the source-grep assertions at
  `tests/test_api.py:758-788`. They are change detectors: they pass if the code
  they describe is commented out, fail on any rename, and demonstrably did not
  catch C2 in the very file they claim to test. Two of them also use a
  CWD-relative path, so they only pass when pytest runs from the repo root.
- Replace them with a packaging smoke test in CI: the `package` job already
  runs the build; with the hookspath fixed it becomes a real signal. Add an
  assertion that the built tree contains `static/app.js`, `static/output.css`,
  and the vendored icon font — the data files most likely to go missing.
- `test_package_exposes_the_initial_release_version` reads the version from
  `pyproject.toml` instead of hard-coding `"0.1.0"`.
- Fill in `CHANGELOG.md`'s empty `[Unreleased]`: pinning, deletion, session
  naming, audio metering, and the histogram all landed after 0.1.0, and the
  repo declares Keep a Changelog as its convention.
- Ship the real brand mark. `tray.py:98-103` builds a flat green square in code
  and the executable uses PyInstaller's default icon, while
  `static/images/broccoli_icon.svg` exists. Render it to `.ico` at build time
  and use it for both.
- `installer/BroccoliDesktop.iss:44,55-57`: single backslashes in the registry
  paths. Windows collapses the doubled separators, so this is cosmetic — but it
  reads like a C escape leaked into Pascal and costs a reader time.
- `scripts/visual-check.ps1:113`: accept a minimum agent-browser version instead
  of pinning `0.34.0` exactly, so an upgrade does not fail the check.

---

## Phase 4 — Interface, proxy, and documents

**Closes:** I4, I5, I8's UI half, and the review's UX and Minor findings.

### 4a. Focus management

The systemic finding. Focus lands on `<body>` after login → capture, opening a
session from the sidebar, the start-capture redirect to settings, and settings →
capture. Root cause proven for the sidebar: the list is rebuilt from scratch on
selection, so the focused node stops existing.

With 96 focusable elements on screen — about 82 of them sidebar rows and their
menu triggers — and no skip link (the document contains no `<a>` at all), every
action costs a keyboard user the full traversal again. For a screen reader the
view change is not announced at all.

Design:

- A `focusScreen(container)` helper: moves focus to the container's heading, or
  its first focusable control, setting `tabindex="-1"` on the heading so it can
  receive focus without entering the tab order.
- Called at each of the four transitions.
- The session list is patched in place by key instead of rebuilt, so the focused
  row survives selection.
- Add a skip link to the main content as the first focusable element.
- `closeSessionMenu` returns focus to the row instead of calling
  `trigger.blur()` (`app.js:870`).
- A failed login returns focus to the token input.

The rename modal already does all of this correctly — native `<dialog>` with
`showModal()`, focus into the input, Escape closes, focus restored to the
trigger. It is the reference implementation for the rest.

### 4b. Accessible names and structure

- Remove `aria-label="Broccoli access token"` from the token input
  (`index.html:132`). The visible `<legend>` "Token de acesso" already names the
  field; the English override breaks WCAG 2.5.3 Label in Name, so voice control
  cannot address the field and a screen reader announces a different language
  than the screen shows. Wire the helper text with `aria-describedby`.
- Give each post-login screen an `<h1>`. Today the only `<h1>` belongs to the
  hidden login screen and axe reports `page-has-heading-one`.
- Fix the settings heading order: the screen title is `<h3>` above `<h2>`
  section headings.
- Transcript deltas render into an `aria-live="off"` provisional region; only
  final segments reach the `role="log"`. Today a partial delta that grows makes
  a screen reader re-announce the whole utterance as it is spoken
  (`index.html:271`).

### 4c. Layout and copy

- At 375px the session title input collapses to **2 pixels** while
  `Código session-1 · 1 segmentos` takes three lines. Give the title the space,
  and move the session code into the options menu or a tooltip.
- Session rows carry no date, time, or duration. With auto-generated titles and
  a list that reaches 45 entries, the history is not navigable. The API already
  returns `started_at` and `ended_at`; render date and duration per row and
  group by day.
- Pluralize the segment count: "1 segmento", not "1 segmentos".
- `renderConnectionState` (`app.js:1195`) fires its "Reconectando…" toast on
  every render while the state is `reconnecting`, so a backend outage produces
  one every 15 s forever. Fire on the state transition instead.
- `renderSessionDetails` (`app.js:1522`) writes `sessionTitle.value`
  unconditionally, clobbering what the user is typing when a `status` event
  arrives. Skip the write while the field has focus.
- The start-capture redirect announces itself: when the button redirects to
  settings for missing devices, a `role="status"` message on the settings screen
  says why, and focus moves to the microphone selector. Today it is a silent
  screen change — for a screen reader, indistinguishable from a dead button.

### 4d. Localization

The tray reads "Show Broccoli Desktop", "Stop capture", "Quit" with
"Status: Transmitindo" between them; the Win32 dialogs are English; and
`reportError` (`app.js:643`) puts the API's English `detail` straight on screen.
`visual-check.ps1:236` waits on *"The session title is invalid."* in a pt-BR
interface, which is the inconsistency made executable.

Design: the API keeps returning machine-readable English `detail` codes — that
is the right contract for an API — and `app.js` maps them to pt-BR strings
through a lookup table, falling back to a generic message for an unmapped code.
The tray and the Win32 dialogs are translated to pt-BR directly. The visual
check then waits on the pt-BR strings throughout.

### 4e. Proxy, for real

The panel currently collects host, port, username and password, persists the
whole object — password included — to `localStorage`, and nothing on the Python
side reads it.

Design:

- Proxy host, port and username live in `settings.py` alongside the other device
  settings. The **password goes to `CredentialStore`** under its own key, never
  to `localStorage` and never into a payload.
- `Services` builds a proxy URL when a proxy is configured, and passes it to
  `httpx.AsyncClient(proxy=…)` in `remote.py:245-252` and to
  `websockets.connect(proxy=…)` at `remote.py:190`.
- The settings panel loses the demonstration copy and gains a "test connection"
  action that makes one request through the configured proxy and reports the
  result.
- Clearing the proxy deletes the stored password.

### 4f. Documents

- `README.md`: rewrite the sentence forbidding a remote endpoint in build inputs
  so it forbids tokens and transcripts, which is what it actually means. The
  hostname stays embedded, per the decision above.
- `docs/superpowers/specs/2026-08-19-broccoli-desktop-design.md` and
  `…-live-integration-design.md`, and `docs/desktop-live-integration.md`:
  update the sections describing WebRTC VAD gating and capability gating to
  describe continuous PCM with server-side VAD and unconditionally available
  history routes, and record that the "open Broccoli web" action was dropped.
- Document `LISTENING_TRANSCRIPTION_BACKEND`'s desktop-side counterpart if one
  exists; otherwise note its absence.

### 4g. Remaining Minors

- `naming.py:122`: drop the module-level `assert` — it disappears under
  `python -O` and `test_naming.py` already covers the property.
- `tray.py:96`: `controller._icon = …` reaches into a private attribute from
  module scope. Accept the icon in `TrayController.__init__`.
- `capture.py:387-392`: `_pcm_level` sums squares in pure Python over ~96,000
  samples/second. numpy is already in the process via soxr;
  `numpy.frombuffer(pcm, "<i2")` cuts it to noise.
- `runtime.py:583-586`: the port probe binds, reads, and closes, leaving a
  TOCTOU window before uvicorn binds. Retry once on `OSError` rather than
  failing with the generic startup dialog.

---

## Out of scope

- Reconnect-with-backoff beyond what exists. The queue bound in Phase 2 makes a
  slow socket survivable; a richer reconnect policy is separate work.
- Any change to the platform's API contract. Everything here works against the
  backend as it stands today.
- Restoring the "open Broccoli web" action.

## Risks

- **Phase 1 changes how the window boots.** If the token is not threaded through
  correctly the app cannot talk to its own API. The `capability_token: None`
  escape hatch keeps the visual runner working, and the test asserting the
  production path never yields `None` is what stops that hatch from becoming a
  production hole.
- **Phase 2 touches the hot path.** Frame ordering and drop behaviour are the
  things to get wrong. The ordering test and the slow-socket test are the
  guards, and both must be written before the change.
- **Phase 4e is new feature work,** not remediation. It is the one place where
  the plan adds surface rather than removing it, and it should be the last thing
  built.
