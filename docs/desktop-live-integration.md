# Desktop live integration acceptance

This procedure is for the authenticated manual acceptance run. It starts real local services but
does not alter the backend, delete retained records, or put credentials in files.

## Preconditions

- `C:\repos\broccoli\.venv\Scripts\python.exe` and this repository's `.venv` both exist.
- The local backend database, Azure/transcription configuration, tenant/user, and available credit
  are ready for the supplied administrator account.
- A microphone and a WASAPI system-loopback device are available to the desktop process.
- A human operator supplies local administrator credentials only through the headed browser UI.
- Agent-browser 0.34.0 is installed. Do not use a Chrome profile, `--restore`, a state file, HAR,
  video recording, browser auto-connect, or a non-loopback desktop endpoint.

Start the controlled processes in one PowerShell terminal and leave it running until acceptance is
finished:

```powershell
.\scripts\live-integration.ps1 -DesktopPort 8765
```

The runner waits for an unauthenticated platform login page at `127.0.0.1:8000` and for desktop
health at the caller-selected loopback port. It gives only the desktop child process the non-secret
`/ws/listening/` WebSocket-path configuration. Press `Ctrl+C` in that terminal to terminate both
child process trees; it deliberately does not delete the token, Credential Manager entry, session,
or segments.

## Headed agent-browser procedure

Use one named, headed, ephemeral session. The allowlist is limited to loopback and the domains
required for the public media playback. Do not add `--profile` or `--restore`.

```powershell
agent-browser --session broccoli-desktop-live-integration --headed --allowed-domains "127.0.0.1,localhost,www.youtube.com,youtube.com,*.googlevideo.com" open http://127.0.0.1:8000/admin/
```

1. Use `snapshot -i` before every action and take a fresh snapshot after it. Log in to the local
   Django admin with the operator-supplied credentials, then navigate through the UI to `/profile/`.
2. Create a token named `desktop-integration-<timestamp>` with validity `30`, and use the UI copy
   control. While the token modal or field is visible, do not take a screenshot or snapshot, read
   DOM text, use `get value`, collect a HAR, or record video. Close the modal after copying.
3. Open `http://127.0.0.1:8765/` in another tab of the same session. Focus the token field, paste
   with `Control+V`, and submit. Once the capture UI appears, verify that the input is empty without
   printing its value.
4. Select the first visible microphone and the first visible system-loopback device by label. Do
   not record opaque device IDs. Save the settings, return to the capture view, then start capture.
5. In a third tab, play a public spoken-word video at
   `https://www.youtube.com/watch?v=iCvmsMzlF7o` headed for at least 60 seconds. Return to the
   desktop tab and wait up to 90 seconds for `Transmitindo` plus a non-empty live timeline row.
   Do not save a screenshot containing the token or transcript text.
6. Stop capture. Collect `agent-browser errors --json` and
   `agent-browser a11y --tags wcag2a,wcag2aa --json`; console errors or Axe violations fail the
   acceptance. Close the named session when finished.

The expected UI timeline is: ready after login, device selection, `Transmitindo` after capture
starts, at least one live timeline row, and `Parado` after the explicit stop.

## Aggregate persistence check

Read only the newest session UUID, status, and segment count. This query intentionally does not
read transcript text:

```powershell
Set-Location C:\repos\broccoli\broccoli
..\.venv\Scripts\python.exe manage.py shell -c "from listening.models import ListeningSession; s = ListeningSession.objects.order_by('-started_at').first(); print({'uuid_code': str(s.uuid_code), 'status': s.status, 'segment_count': s.segments.count()} if s else None)"
```

Write a sanitized Markdown report under the ignored `artifacts/integration/` directory with this
shape:

```markdown
# Desktop live integration acceptance

- Time window: <start/end only>
- Preconditions: <available or the non-secret blocking prerequisite>
- Channels selected: microphone=<available/unavailable>, system-loopback=<available/unavailable>
- State transition: ready -> streaming -> stopped
- Persistence: UUID=<uuid>, status=<status>, segment_count=<count>
- Browser checks: console_errors=0, axe_violations=0
- Outcome: pass | blocked | fail
```

Never include administrator credentials, a token, Credential Manager contents, transcript text,
audio/PCM, a HAR, or a token/transcript screenshot. If an Azure, credit, user/tenant, WASAPI, or
media prerequisite is missing, record the non-secret prerequisite and stop the acceptance rather
than changing backend code or deleting retained data.
