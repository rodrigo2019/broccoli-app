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

if ($FakeRemote -or $BrowserOnly) {
    throw "-FakeRemote and -BrowserOnly must be used together."
}

& .\.venv\Scripts\python.exe -m broccoli_desktop
exit $LASTEXITCODE
