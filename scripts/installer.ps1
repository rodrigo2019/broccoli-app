[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$installerOutput = Join-Path $projectRoot "dist\installer"
$specification = Join-Path $projectRoot "installer\BroccoliDesktop.iss"

if (Test-Path -LiteralPath $installerOutput) {
    Remove-Item -LiteralPath $installerOutput -Recurse -Force
}

& (Join-Path $PSScriptRoot "package.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "The package build failed with exit code $LASTEXITCODE."
}

$isccCommand = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
$isccPath = if ($null -eq $isccCommand) { $null } else { $isccCommand.Source }
if ($null -eq $isccPath) {
    foreach ($candidate in @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )) {
        if (Test-Path -LiteralPath $candidate) {
            $isccPath = $candidate
            break
        }
    }
}
if ([string]::IsNullOrWhiteSpace($isccPath)) {
    throw "Inno Setup 6 ISCC.exe was not found. Install Inno Setup 6 before building the installer."
}

& $isccPath $specification
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}
