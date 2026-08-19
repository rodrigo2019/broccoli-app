[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildDirectory = Join-Path $projectRoot "build\BroccoliDesktop"
$distDirectory = Join-Path $projectRoot "dist\BroccoliDesktop"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$specification = Join-Path $projectRoot "installer\BroccoliDesktop.spec"

foreach ($directory in @($buildDirectory, $distDirectory)) {
    if (Test-Path -LiteralPath $directory) {
        Remove-Item -LiteralPath $directory -Recurse -Force
    }
}

& (Join-Path $PSScriptRoot "build-css.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "The CSS build failed with exit code $LASTEXITCODE."
}

& $python -m PyInstaller --noconfirm --clean $specification
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}
