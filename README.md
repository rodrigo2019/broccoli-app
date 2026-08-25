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

## Building a copy to hand to someone

Produces a zip that runs on another Windows 11 x64 machine with no Python
installed:

```powershell
.\scripts\build-app.ps1
```

It rebuilds the UI and the package, then **starts the executable and waits for
its local service to answer** before writing
`dist\BroccoliDesktop-<version>-win-x64.zip`. That check is the point: two
startup defects once shipped because every check stopped at "the build
succeeded", and running the executable from a shell hides one of them, so the
check launches it the way a shortcut does. CI runs the same script.

Whoever receives the zip extracts **the whole folder** and runs
`BroccoliDesktop.exe` from inside it. The executable needs the `_internal`
folder beside it; dragging the `.exe` out on its own fails with "Failed to load
Python DLL". Microsoft Edge WebView2 Runtime must be present, which it is by
default on Windows 11.

To check an executable that is already built, without rebuilding:

```powershell
.\scripts\verify-package.ps1
```

## Windows installer

For a real installation -- shortcuts, an uninstaller, a WebView2 check -- build
the installer on Windows 11 x64 with Python 3.12, Node.js/npm, uv, and
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

## Interface language

English, Portuguese (Brazil) and German. The wording lives in
`broccoli_desktop/locales/*.json`, one file per language, each holding two flat
maps: `ui`, keyed by a semantic name, and `api`, keyed by the English
`ApiError` detail the local service sends -- that detail is a wire contract and
stays English, the translation of it is what the user reads.

There is one copy of every string and three surfaces read it. The `/` route
puts all three catalogs into the served document, so the window has them before
it paints and changing the language needs neither a request nor a reload; the
notification-area menu and the Windows dialogs read the same stored choice
through `i18n.Translator`, which re-reads it per call so they follow a change
made in the settings screen without a restart. The choice is saved to
`ui-settings.json` beside the other selections under `%LOCALAPPDATA%`; with
nothing saved -- a fresh install, or "Follow Windows" in the picker -- the
Windows display language decides, falling back to English.

Adding a string means adding the same key to all three files: a test compares
the key sets, another pins every literal in `index.html` to the English
catalog, and a third checks that every `ApiError` detail the service can send
is answered in every language.

The automatic titles for unnamed sessions (`naming.py`) are deliberately not
translated -- see the note in that module.

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
