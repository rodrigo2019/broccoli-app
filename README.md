# Broccoli Desktop

Broccoli Desktop is a Windows client for live meeting transcription. It connects to
an externally provisioned Broccoli backend; this repository does not run, migrate,
or deploy that backend.

## Prerequisites

- Python 3.12.10
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Node.js and npm

## Development

Sync the Python environment:

```powershell
.\scripts\sync.ps1
```

Install the UI dependencies and build the Tailwind stylesheet:

```powershell
Set-Location ui
npm ci
npm run build:css
Set-Location ..
```

Run validation:

```powershell
.\scripts\check.ps1
```

## Backend WebSocket configuration

Before launching the desktop client, obtain the exact WebSocket path from the
backend owner and set it locally. The path is non-secret, but it must be supplied
by the deployed backend; Broccoli Desktop never guesses a remote route.

```powershell
$env:BROCCOLI_DESKTOP_WEBSOCKET_PATH = "/path-provided-by-the-backend-owner"
broccoli-desktop --local
```

The normal desktop runtime will display a local startup error when this setting
is missing or is not an absolute path. The fake browser-only visual runner does
not need a production remote path.

The `broccoli-desktop` command supports `--local` for
`http://127.0.0.1:8000` and `--dev` for a development URL supplied by local build
configuration. Production builds use the official remote Broccoli backend, which is
an external prerequisite.
