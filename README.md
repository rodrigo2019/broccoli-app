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

Install the UI dependencies, then build the Tailwind stylesheet and vendor the
Bootstrap Icons font into the packaged static tree:

```powershell
Set-Location ui
npm ci
npm run build
Set-Location ..
```

Run validation:

```powershell
.\scripts\check.ps1
```

## Windows installer

Build the installer on Windows 11 x64 with Python 3.12, Node.js/npm, uv, and
[Inno Setup 6](https://jrsoftware.org/isinfo.php) available locally. The build
regenerates the ignored CSS and Windows package outputs from the committed locks:

```powershell
.\scripts\installer.ps1
```

The installer is written to
`dist\installer\BroccoliDesktop-0.1.0-setup.exe`. It installs per user, creates
Start Menu and desktop shortcuts named **Broccoli Desktop**, and includes an
uninstaller. Microsoft Edge WebView2 Runtime must already be installed; the
installer stops before installation when it is unavailable.

The packaged application embeds the backend hostnames and the Listening
WebSocket path by design (see `config.py`); do not include a token or a
production transcript in build inputs, CI configuration, or release artifacts.

## Selecting an environment

`broccoli-desktop` runs against production by default. `--dev` selects the
development deployment and `--local` a platform checkout served on
`http://127.0.0.1:8000`:

```powershell
broccoli-desktop --local
```

The local port is the one setting that varies per machine, so `--local` takes
it as an argument. Nothing else needs configuring -- no environment variable is
read at startup:

```powershell
broccoli-desktop --local 9000
```
