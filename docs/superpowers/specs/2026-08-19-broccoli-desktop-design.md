# Broccoli Desktop — Design

**Date:** 2026-08-19  
**Status:** Proposed for review  
**Scope:** Windows desktop MVP for the Listening feature. The Broccoli backend is an external
contract and is not changed by this work.

## Summary

Broccoli Desktop is a Windows 11 desktop client for live meeting transcription. It captures the selected microphone and selected system-output device on separate channels, resamples each to continuous 24 kHz PCM16 and sends it to Broccoli over the existing listening WebSocket — voice-activity detection runs server-side, not on the client — and displays a session's live transcription.

The user authenticates by pasting an existing Broccoli API token. The token is stored in Windows Credential Manager and is never exposed to the embedded web UI. The app also provides a server-backed library of the user's retained sessions. A user may reopen a historical session, preserving its code and transcript timeline, then continue capturing into it.

This design assumes the listening backend contract in “External backend prerequisites” is delivered and reachable. Broccoli Desktop neither modifies nor deploys that contract.

## Goals

- Deliver a polished, installable Windows 11 x64 MVP named **Broccoli Desktop**.
- Capture microphone and WASAPI loopback audio in separate mic and system channels.
- Show both partial and confirmed transcript text with channel attribution during a meeting.
- Let a token-authenticated user browse, search, title, and reopen their retained sessions.
- Keep the session code easy to copy so the user can attach it to a chat.
- Preserve the existing privacy promise: no meeting audio is written to local disk or persisted by Broccoli.

## Non-goals

- macOS, Linux, Windows 10, 32-bit Windows, and automatic updates.
- A chat picker or chat-attachment UI in the desktop app.
- Local transcript archival beyond the retention policy configured by the tenant.
- Audio compression, speaker diarization beyond the two capture channels, or meeting summaries.
- Persisting partial transcript events.
- Any change, migration, test, deployment, or operational work in C:\\repos\\broccoli.

## Product decisions

- The product name is **Broccoli Desktop**.
- The official Broccoli URL is embedded in production builds. --local targets http://127.0.0.1:8000; --dev reads the development URL from local build configuration.
- The login credential is a Broccoli API token, stored through keyring in Windows Credential Manager. Logout deletes it. Invalid, expired, or revoked tokens are deleted and return the user to login.
- The language hint is omitted: the transcription model performs automatic language detection.
- Users select both microphone and system-output devices. The last valid selections are reused; missing devices require a new selection before capture starts.
- Closing the window minimizes it to the system tray. Tray actions are show window, status, stop capture, and quit. Quitting during capture requires confirmation.
- A persistent recording/transcription notice is visible whenever capture is active. There is no confirmation modal in this MVP.
- An active session is limited to four hours per capture period. Reopening the same historical session starts a fresh four-hour period while retaining its transcript and UUID.
- On network failure, the client retains at most ten seconds of continuously captured audio in a single bounded per-run queue, retries with backoff, and automatically reopens the same session if the original active window expires. If the queue fills because the socket has fallen behind, the oldest frame is dropped and the user is told; capture is never blocked waiting on the network.
- The central pane's session-code affordance is copy-only. An earlier "open Broccoli web" action tied to the session code was dropped before release and does not ship.

## User experience

The main screen follows the **Caderno de reunião** layout:

- The left pane contains a searchable, cursor-paginated session library, initially loading the 30 newest retained sessions. It highlights the open session and exposes new-session and resume actions.
- The central pane contains the optional editable title, capture and connection state, selected devices, persistent recording notice, a session-code copy button, and a timestamped transcript timeline.
- Creating a session uses a compact panel with optional title and the microphone/system selectors. Resuming a session uses its saved devices when available and otherwise asks for replacements.
- Confirmed entries identify “Você” for mic and “Participantes” for system. Per-channel partial text is shown in italics and replaced by the matching confirmed entry. Auto-scroll follows live text until the user manually scrolls away.
- Stopping capture ends only the current capture period. The selected session remains visible and can be resumed.

Failures use clear status messages and recovery actions. Device loss — at startup or mid-capture — pauses on the device-selection screen until a replacement device is chosen. Token failures return to login. Credit denial and remote termination stop capture. Network failures show reconnecting state while the client retries. No failure path writes audio or transcript copies locally.

## Local architecture

The packaged Python process starts a FastAPI service bound only to 127.0.0.1, then opens its static Tailwind/daisyUI interface in PyWebView using the Windows WebView2 engine. The UI uses plain ES modules and calls only the local FastAPI API; it cannot read the remote token or open the remote listening WebSocket. Every `/api/*` request must also pass an Origin check and present a capability token generated fresh at each launch: the token is carried once in the window URL's `?k=` query parameter, read and stripped by the page's own script, then attached to later requests (as a header where possible, as `?k=` for WebSocket/EventSource connections that cannot set one) — so another local process cannot reach the API merely by knowing the port.

The FastAPI process owns these independently testable components:

- CredentialStore reads and deletes the Windows Credential Manager value; reads run off the event loop so a slow vault lookup cannot freeze the local API.
- BroccoliClient calls the token-authenticated desktop REST API and opens the remote WebSocket, routed through the configured proxy when one is set.
- SessionController coordinates session state, reconnect attempts, offsets, title updates, and local UI events. Captured audio for the active run passes through one bounded queue and one sender task; if the socket falls behind, the oldest frame is dropped and the user is told.
- AudioCapture enumerates devices through PyAudioWPatch; enumeration is cached for a short TTL and runs off the event loop so it cannot freeze the local API.
- AudioPipeline accepts 20 ms 48 kHz PCM blocks continuously — there is no client-side VAD gate — resamples them to mono PCM16 at 24 kHz with soxr, then batches 100 ms protocol frames. The backend applies VAD server-side.
- TrayController owns the Windows notification-area actions and keeps capture alive while the UI window is hidden.

All interface strings — screens, tray menu and status, and native Win32 dialogs — are Brazilian Portuguese. English `detail` strings returned by the remote API are mapped to pt-BR client-side before display.

Raw audio exists in capture buffers and the bounded reconnect buffer only. The UI receives no raw audio and the app writes no audio file.

## External backend prerequisites

The backend is owned and delivered outside this plan. Before Broccoli Desktop is connected to a real environment, it must provide the following token-authenticated contract. Every route requires Authorization: Token <key> and returns only sessions owned by the caller in the token's tenant:

- GET /api/listening/desktop/sessions/?cursor=&q= lists sessions by newest activity, cursor
  paginated, with q filtering title and date text.
- GET /api/listening/desktop/sessions/<uuid_code>/ returns one session's metadata and status.
- GET /api/listening/desktop/sessions/<uuid_code>/segments/?cursor= returns its transcript
  segments in timeline order.
- PATCH /api/listening/desktop/sessions/<uuid_code>/ accepts the optional title.

The contract exposes an optional title of at most 120 characters and a capture-period start. Its resume handshake accepts a user-owned retained session in a terminal state, reactivates it, and starts a fresh four-hour capture period after the usual credit check. It continues to enforce one active session per user.

session.started does not itself carry a resume offset. On a new session the client's monotonic capture clock starts at zero. On a resumed session, the client reads the session's last transcript-segment page (GET .../segments/?cursor=, jumping straight to the final page boundary rather than walking the whole transcript) and uses the greatest ended-offset among the segments it contains as the base it adds to its own capture clock, ensuring historical and resumed speech never overlap in the global transcript timeline.

The contract forwards an ephemeral event:

~~~json
{
  "type": "transcript.delta",
  "channel": "mic",
  "utterance_id": "opaque-id",
  "text": "partial words",
  "started_offset_ms": 12345
}
~~~

transcript.segment includes the same utterance_id together with its final text and offsets. The client replaces the matching delta; only final segments are stored in TranscriptSegment. The Azure adapter omits language from its transcription-session request when the client supplied no hint.

The realtime Azure deployment remains an external release gate. Its owner must record and verify the actual Azure event format before the connected client is distributed.

## Repository, packaging, and quality

The new repository follows the main project's conventions where applicable: Python 3.12, Hatchling, pyproject.toml, committed uv.lock, dependency group for development tools, Ruff, Pytest, LF-only text files, and a Keep a Changelog changelog. The frontend has its own committed npm lock for Tailwind 4 and daisyUI 5.

Pinned runtime dependencies are FastAPI 0.141.1, Uvicorn 0.52.4, PyWebView 6.2.1, websockets 17.0.1, httpx 0.28.1, PyAudioWPatch 0.2.12.8, soxr 1.1.0, numpy 2.5.2, keyring 25.7.0, pystray 0.19.5, and PyInstaller 6.22.2. There is no client-side VAD dependency; VAD runs server-side. uv lock is the authority for all transitive versions.

PyInstaller produces a onedir Windows bundle containing static assets and native audio libraries. Inno Setup produces the distributable .exe, shortcuts, uninstaller, and WebView2 availability check. No updater is shipped.

Testing is offline by default. Unit and FastAPI tests cover credentials, URL selection, resampling, protocol framing, the bounded audio queue, local API state, and tray/session controllers through fakes. Contract tests use a fake implementation of the external desktop API and WebSocket, including title updates, pagination, historical resume, offsets, deltas, token failures, and remote termination. Manual Windows acceptance tests cover real microphone/loopback capture, device removal, tray lifecycle, token revocation, network recovery, a resumed historical session, and a clean installer against an environment whose backend owner confirms the prerequisites.

CI runs on Windows and performs frozen dependency sync, lint, tests, CSS build, PyInstaller bundle, and installer artifact generation.
