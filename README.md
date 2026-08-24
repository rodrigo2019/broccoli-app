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

## Application icon

The window, the notification area, the executable, and the installer all read
`broccoli_desktop\static\images\broccoli_icon.ico`, which is committed. It is
generated from `broccoli_icon.svg` by a script that rasterizes the logo with an
installed Chrome or Edge; rerun it after changing the logo and commit the
result:

```powershell
.\.venv\Scripts\python.exe scripts\build_icon.py
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
WebSocket path by design (see `config.py`), along with the default proxy
configuration script address (see `settings.py`); do not include a token or a
production transcript in build inputs, CI configuration, or release artifacts.

## Proxy

The settings screen configures the proxy either manually, with a host and port,
or from an automatic configuration script (PAC). In script mode Windows itself
downloads and evaluates the script through `WinHttpGetProxyForUrl` and names a
proxy for the backend URL, so no JavaScript engine is bundled; the username and
password from the same panel are then applied to whatever proxy it chose. The
script address arrives filled in with the address Windows uses under Settings >
Network & Internet > Proxy, but nothing is routed anywhere until the proxy is
enabled.

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
