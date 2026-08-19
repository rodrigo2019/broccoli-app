[CmdletBinding()]
param(
    [switch]$FakeRemote,
    [switch]$BrowserOnly,
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

if ($FakeRemote -and $BrowserOnly) {
    & .\.venv\Scripts\python.exe -m tests.visual_server --port $Port
    exit $LASTEXITCODE
}

if ($FakeRemote) {
    throw "-FakeRemote requires -BrowserOnly."
}

if ($BrowserOnly) {
    & .\.venv\Scripts\python.exe -m broccoli_desktop.browser_only --port $Port
    exit $LASTEXITCODE
}

& .\.venv\Scripts\python.exe -m broccoli_desktop
exit $LASTEXITCODE
