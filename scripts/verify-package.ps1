[CmdletBinding()]
param(
    [string]$ExecutablePath,
    [int]$TimeoutSeconds = 90
)

# Run the packaged executable and wait for its local service to answer.
#
# Two startup defects shipped unnoticed because every check stopped at "the
# build succeeded": the archive was missing broccoli_desktop.runtime, and
# uvicorn's logging setup called isatty() on the None stdout a windowed build
# has. Neither is visible until the executable actually runs.
#
# Start-Process is the point. Launching the exe from a shell hands it the
# shell's stdout and hides the second defect completely; Start-Process goes
# through ShellExecute, the way a shortcut or a double click does, with no
# console and no standard handles.
#
# Reaching /health proves the runtime imported, the logging configuration
# survived and the loopback service is up. It says nothing about the native
# window, which opens afterwards and needs a desktop session -- but both
# defects landed before that point.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ExecutablePath)) {
    $ExecutablePath = Join-Path $projectRoot "dist\BroccoliDesktop\BroccoliDesktop.exe"
}
if (-not (Test-Path -LiteralPath $ExecutablePath)) {
    throw "There is no packaged executable at $ExecutablePath. Build one first with scripts\package.ps1."
}

Write-Host "Starting $ExecutablePath with no console, the way a shortcut does..."
$process = Start-Process -FilePath $ExecutablePath -PassThru
try {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $port = $null
    while ($null -eq $port -and (Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "The packaged application exited with code $($process.ExitCode) instead of serving its local API."
        }
        $listening = @(
            Get-NetTCPConnection -OwningProcess $process.Id -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $_.LocalAddress -eq "127.0.0.1" }
        )
        if ($listening.Count -gt 0) {
            $port = $listening[0].LocalPort
        } else {
            Start-Sleep -Milliseconds 500
        }
    }
    if ($null -eq $port) {
        # The usual shape of a failure here: the executable put up an error
        # dialog and is sitting on it, so it never exits and never listens.
        throw "The packaged application never opened a loopback port within $TimeoutSeconds seconds."
    }

    $health = Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 10
    if ($health.status -ne "ok") {
        throw "The packaged application answered /health with $($health | ConvertTo-Json -Compress)."
    }
    Write-Host "The packaged application served /health on port $port."
} finally {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        # Wait for it, rather than assuming Stop-Process is synchronous. Until
        # the process is gone it still holds open handles on everything it
        # loaded out of _internal, and the caller's next move is usually to zip
        # that folder -- which fails on the first .pyd still in use.
        if (-not $process.WaitForExit(10000)) {
            throw "The packaged application is still running after being stopped; its files are still locked."
        }
    }
}
