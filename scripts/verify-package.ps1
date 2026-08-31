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
#
# Every loopback port the process listens on is asked, rather than the first
# one found. The splash screen's Tcl interpreter opens an IPC server on
# 127.0.0.1 before Python starts, so the first listening port is reliably not
# uvicorn's, and asking that one for /health only times out. A port that does
# not answer HTTP is simply not the one; the answer decides, not the order.

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
    $ports = @()
    while ($null -eq $port -and (Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "The packaged application exited with code $($process.ExitCode) instead of serving its local API."
        }
        $ports = @(
            Get-NetTCPConnection -OwningProcess $process.Id -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $_.LocalAddress -eq "127.0.0.1" } |
                ForEach-Object { $_.LocalPort } |
                Sort-Object -Unique
        )
        foreach ($candidate in $ports) {
            try {
                # Short, because most of these attempts are meant to fail: the
                # splash's IPC socket accepts the connection and then never
                # answers, and waiting ten seconds on each one would spend the
                # whole budget before reaching uvicorn's port.
                $answer = Invoke-RestMethod "http://127.0.0.1:$candidate/health" -TimeoutSec 2
            } catch {
                continue
            }
            if ($answer.status -ne "ok") {
                throw "The packaged application answered /health on port $candidate with $($answer | ConvertTo-Json -Compress)."
            }
            $port = $candidate
            break
        }
        if ($null -eq $port) {
            Start-Sleep -Milliseconds 500
        }
    }
    if ($null -eq $port) {
        # The usual shape of a failure here: the executable put up an error
        # dialog and is sitting on it, so it never exits and never serves.
        $listed = if ($ports.Count -gt 0) { $ports -join ", " } else { "none" }
        throw "The packaged application never answered /health within $TimeoutSeconds seconds (loopback ports seen: $listed)."
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
