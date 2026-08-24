[CmdletBinding()]
param(
    [switch]$SkipVerify
)

# Build the application and leave a zip that can be handed to someone else.
#
#     .\scripts\build-app.ps1
#
# Three steps: package.ps1 produces the onedir tree (rebuilding the UI from the
# committed locks on the way), verify-package.ps1 proves the result actually
# starts, and the zip is what travels.
#
# The zip carries the tree, not just the executable: BroccoliDesktop.exe is
# useless without the _internal folder beside it, and dragging the .exe out on
# its own fails with "Failed to load Python DLL". Whoever receives it extracts
# the whole folder and runs the executable from inside it.
#
# For an installer instead -- Start Menu and desktop shortcuts, an uninstaller,
# a WebView2 check -- use scripts\installer.ps1, which needs Inno Setup 6.

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$distributionRoot = Join-Path $projectRoot "dist"
$packagedTree = Join-Path $distributionRoot "BroccoliDesktop"

if (-not (Test-Path -LiteralPath $python)) {
    throw "There is no virtual environment at $python. Run scripts\sync.ps1 first."
}

# Asking the package rather than repeating the number: test_version.py already
# ties broccoli_desktop.__version__ to pyproject.toml, so this cannot drift.
$version = & $python -c "import broccoli_desktop; print(broccoli_desktop.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "Could not read the application version (exit code $LASTEXITCODE)."
}

& (Join-Path $PSScriptRoot "package.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "The package build failed with exit code $LASTEXITCODE."
}

$archive = Join-Path $distributionRoot "BroccoliDesktop-$version-win-x64.zip"
# Compress first, verify second, and only then put the archive under its real
# name. Two things fall out of that order.
#
# The tree has not been run yet, so nothing holds a handle on it. Verifying
# first meant zipping a folder whose DLLs the just-stopped process still had
# open -- Windows does not release those the moment the process is killed, and
# Compress-Archive failed on whichever file the scan had not let go of yet.
#
# And the final name only ever appears on an archive that passed, without the
# previous one being deleted first to make room for something that might not
# arrive. Still a .zip name: Compress-Archive refuses any other extension.
$pending = Join-Path $distributionRoot "BroccoliDesktop-$version-win-x64.partial.zip"
if (Test-Path -LiteralPath $pending) {
    Remove-Item -LiteralPath $pending -Force
}
# -ErrorAction Stop because Compress-Archive reports a locked file by writing a
# non-terminating error from inside its own module, which $ErrorActionPreference
# in this scope does not reach.
Compress-Archive -Path (Join-Path $packagedTree "*") -DestinationPath $pending `
    -CompressionLevel Optimal -ErrorAction Stop
if (-not (Test-Path -LiteralPath $pending)) {
    throw "Compress-Archive reported success but wrote no archive to $pending."
}

try {
    if ($SkipVerify) {
        # Only for a machine that cannot run the app at all -- no desktop
        # session, no WebView2. The check is the difference between "it
        # compiled" and "it starts", which is what shipped broken twice.
        Write-Warning "Skipping the startup check: this zip has not been shown to start."
    } else {
        & (Join-Path $PSScriptRoot "verify-package.ps1")
    }
} catch {
    Remove-Item -LiteralPath $pending -Force -ErrorAction SilentlyContinue
    throw
}

Move-Item -LiteralPath $pending -Destination $archive -Force

$size = [Math]::Round((Get-Item -LiteralPath $archive).Length / 1MB, 1)
Write-Host ""
Write-Host "Wrote $archive ($size MB)."
Write-Host "Extract the whole folder on the target machine and run BroccoliDesktop.exe from inside it."
