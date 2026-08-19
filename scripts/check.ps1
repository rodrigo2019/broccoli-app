$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

Invoke-NativeCommand -Description "Ruff format" -Command {
    & .\.venv\Scripts\ruff.exe format --check .
}
Invoke-NativeCommand -Description "Ruff check" -Command {
    & .\.venv\Scripts\ruff.exe check .
}
Invoke-NativeCommand -Description "pytest" -Command {
    & .\.venv\Scripts\python.exe -m pytest
}
