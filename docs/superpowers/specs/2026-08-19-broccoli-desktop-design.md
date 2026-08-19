# Broccoli Desktop — Design

**Date:** 2026-08-19  
**Status:** Proposed for review  
**Scope:** Windows desktop MVP for the Listening feature, plus the required Broccoli backend extensions.

## Summary

Broccoli Desktop is a Windows 11 desktop client for live meeting transcription. It captures the selected microphone and selected system-output device on separate channels, sends VAD-gated audio to Broccoli over the existing listening WebSocket, and displays a session's live transcription.

The user authenticates by pasting an existing Broccoli API token. The token is stored in Windows Credential Manager and is never exposed to the embedded web UI. The app also provides a server-backed library of the user's retained sessions. A user may reopen a historical session, preserving its code and transcript timeline, then continue capturing into it.

## Goals

- Deliver a polished, installable Windows 11 x64 MVP named **Broccoli Desktop**.
- Capture microphone and WASAPI loopback audio in separate mic and system channels.
- Show both partial and confirmed transcript text with channel attribution during a meeting.
- Let a token-authenticated user browse, search, title, and reopen their retained sessions.
- Keep the session code easy to copy and open Broccoli web so the user can attach it to a chat.
- Preserve the existing privacy promise: no meeting audio is written to local disk or persisted by Broccoli.

## Non-goals

- macOS, Linux, Windows 10, 32-bit Windows, and automatic updates.
- A chat picker or chat-attachment UI in the desktop app.
- Local transcript archival beyond the retention policy configured by the tenant.
- Audio compression, speaker diarization beyond the two capture channels, or meeting summaries.
- Persisting partial transcript events.

## Product decisions

- The product name is **Broccoli Desktop**.
- The official Broccoli URL is embedded in production builds. --local targets http://127.0.0.1:8000; --dev reads the development URL from local build configuration.
- The login credential is a Broccoli API token, stored through keyring in Windows Credential Manager. Logout deletes it. Invalid, expired, or revoked tokens are deleted and return the user to login.
- The language hint is omitted: the transcription model performs automatic language detection.
- Users select both microphone and system-output devices. The last valid selections are reused; missing devices require a new selection before capture starts.
- Closing the window minimizes it to the system tray. Tray actions are show window, status, stop capture, and quit. Quitting during capture requires confirmation.
- A persistent recording/transcription notice is visible whenever capture is active. There is no confirmation modal in this MVP.
- An active session is limited to four hours per capture period. Reopening the same historical session starts a fresh four-hour period while retaining its transcript and UUID.
- On network failure, the client retains at most ten seconds of VAD-gated audio in memory, retries with backoff, and automatically reopens the same session if the original active window expires.

## User experience

The main screen follows the **Caderno de reunião** layout:

- The left pane contains a searchable, cursor-paginated session library, initially loading the 30 newest retained sessions. It highlights the open session and exposes new-session and resume actions.
- The central pane contains the optional editable title, capture and connection state, selected devices, persistent recording notice, session-code copy button, open-Broccoli action, and a timestamped transcript timeline.
- Creating a session uses a compact panel with optional title and the microphone/system selectors. Resuming a session uses its saved devices when available and otherwise asks for replacements.
- Confirmed entries identify “Você” for mic and “Participantes” for system. Per-channel partial text is shown in italics and replaced by the matching confirmed entry. Auto-scroll follows live text until the user manually scrolls away.
- Stopping capture ends only the current capture period. The selected session remains visible and can be resumed.

Failures use clear status messages and recovery actions. Device loss pauses startup until a device is selected. Token failures return to login. Credit denial and remote termination stop capture. Network failures show reconnecting state while the client retries. No failure path writes audio or transcript copies locally.

## Local architecture

The packaged Python process starts a FastAPI service bound only to 127.0.0.1, then opens its static Tailwind/daisyUI interface in PyWebView using the Windows WebView2 engine. The UI uses plain ES modules and calls only the local FastAPI API; it cannot read the remote token or open the remote listening WebSocket.

The FastAPI process owns these independently testable components:

- CredentialStore reads and deletes the Windows Credential Manager value.
- BroccoliClient calls the token-authenticated desktop REST API and opens the remote WebSocket.
- SessionController coordinates session state, reconnect attempts, offsets, title updates, and local UI events.
- AudioCapture enumerates devices and captures selected inputs through PyAudioWPatch.
- AudioPipeline accepts 20 ms 48 kHz PCM blocks, applies WebRTC VAD, resamples speech to mono PCM16 at 24 kHz with soxr, then creates 100 ms protocol frames.
- TrayController owns the Windows notification-area actions and keeps capture alive while the UI window is hidden.

Raw audio exists in capture buffers and the bounded reconnect buffer only. The UI receives no raw audio and the app writes no audio file.

## Broccoli backend contract

Existing browser-facing listening routes remain session-authenticated. Add a token-authenticated,
user-and-tenant-scoped desktop namespace. Every route requires Authorization: Token <key> and
returns only sessions owned by the caller in the token's tenant:

- GET /api/listening/desktop/sessions/?cursor=&q= lists sessions by newest activity, cursor
  paginated, with q filtering title and date text.
- GET /api/listening/desktop/sessions/<uuid_code>/ returns one session's metadata and status.
- GET /api/listening/desktop/sessions/<uuid_code>/segments/?cursor= returns its transcript
  segments in timeline order.
- PATCH /api/listening/desktop/sessions/<uuid_code>/ accepts the optional title.

Extend ListeningSession with title (optional, maximum 120 characters) and capture_started_at. A
resume handshake for a user-owned retained session in any terminal state may reactivate it, clearing
the current terminal state and setting a new capture period. Credit checks still occur at handshake.
The existing partial unique constraint continues to enforce one active session per user.

session.started adds next_offset_ms, the maximum completed segment offset for that session. The desktop client adds this base to its monotonic capture clock, ensuring historical and resumed speech never overlap in the global transcript timeline.
On a new session the base is zero; on a resumed session the first client offset is
next_offset_ms plus elapsed capture time.

The backend forwards an ephemeral event:

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

The realtime Azure deployment remains a release gate. The existing Spike 0 must record and verify the actual Azure event format before the connected client is distributed.

## Repository, packaging, and quality

The new repository follows the main project's conventions where applicable: Python 3.12, Hatchling, pyproject.toml, committed uv.lock, dependency group for development tools, Ruff, Pytest, LF-only text files, and a Keep a Changelog changelog. The frontend has its own committed npm lock for Tailwind 4 and daisyUI 5.

Candidate pinned runtime dependencies are FastAPI 0.141.1, Uvicorn 0.52.4, PyWebView 6.2.1, websockets 17.0.1, PyAudioWPatch 0.2.12.8, webrtcvad-wheels 2.0.14, soxr 1.1.0, keyring 25.7.0, pystray 0.19.5, and PyInstaller 6.22.2. uv lock is the authority for all transitive versions.

PyInstaller produces a onedir Windows bundle containing static assets and native audio libraries. Inno Setup produces the distributable .exe, shortcuts, uninstaller, and WebView2 availability check. No updater is shipped.

Testing is offline by default. Unit and FastAPI tests cover credentials, URL selection, VAD, resampling, protocol framing, bounded reconnects, local API state, and tray/session controllers through fakes. Django tests cover desktop API authorization, titles, pagination, resume lifecycle, four-hour capture-period enforcement, offsets, and delta forwarding. Manual Windows acceptance tests cover real microphone/loopback capture, device removal, tray lifecycle, token revocation, network recovery, a resumed historical session, and a clean installer.

CI runs on Windows and performs frozen dependency sync, lint, tests, CSS build, PyInstaller bundle, and installer artifact generation.
