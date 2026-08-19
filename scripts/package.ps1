[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$buildRoot = Join-Path $projectRoot "build"
$distRoot = Join-Path $projectRoot "dist"
$buildDirectory = Join-Path $buildRoot "BroccoliDesktop"
$distDirectory = Join-Path $distRoot "BroccoliDesktop"
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

& $python -m PyInstaller --noconfirm --clean --workpath $buildRoot --distpath $distRoot $specification
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}
