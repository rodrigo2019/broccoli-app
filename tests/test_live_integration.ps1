[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $projectRoot "scripts\live-integration.ps1"

try {
    . $runner -DesktopPort 8000
} catch {
    if ($_.Exception.Message -ne "DesktopPort must not be 8000 because the local platform uses 127.0.0.1:8000.") {
        throw
    }
}

$probe = "import os, sys; raise SystemExit(0 if sys.argv[1] == 'two words' and os.getcwd() == sys.argv[2] and os.environ['BROCCOLI_DESKTOP_WEBSOCKET_PATH'] == '/ws/listening/' else 1)"
$process = Start-ChildProcess `
    -FilePath $desktopPython `
    -Arguments @("-c", $probe, "two words", $desktopRoot) `
    -WorkingDirectory $desktopRoot `
    -Environment @{ "BROCCOLI_DESKTOP_WEBSOCKET_PATH" = "/ws/listening/" }

$process.WaitForExit()
if ($process.ExitCode -ne 0) {
    throw "The child process did not receive its quoted arguments, working directory, and child-only WebSocket path."
}

Write-Output "live-integration child process construction passed."
