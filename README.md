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

The future `broccoli-desktop` command will support `--local` for
`http://127.0.0.1:8000` and `--dev` for a development URL supplied by local build
configuration. Production builds use the official remote Broccoli backend, which is
an external prerequisite.
