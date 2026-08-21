[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$DesktopPort = 8765
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$desktopRoot = Split-Path -Parent $PSScriptRoot
$desktopPython = Join-Path $desktopRoot ".venv\Scripts\python.exe"
$backendRoot = "C:\repos\broccoli"
$backendProject = Join-Path $backendRoot "broccoli"
$backendPython = Join-Path $backendRoot ".venv\Scripts\python.exe"
$backendProcess = $null
$desktopProcess = $null

function Assert-RequiredFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Description)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description was not found at '$Path'."
    }
}

function ConvertTo-WindowsCommandLineArgument {
    param([Parameter(Mandatory = $true)][string]$Argument)

    if ($Argument.Length -eq 0) {
        return '""'
    }
    if ($Argument -notmatch '[\s"]') {
        return $Argument
    }

    $quoted = [System.Text.StringBuilder]::new('"')
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$quoted.Append((-join ('\' * (($backslashes * 2) + 1))))
            [void]$quoted.Append('"')
        } else {
            [void]$quoted.Append((-join ('\' * $backslashes)));
            [void]$quoted.Append($character)
        }
        $backslashes = 0
    }
    [void]$quoted.Append((-join ('\' * ($backslashes * 2))))
    [void]$quoted.Append('"')
    return $quoted.ToString()
}

function Start-ChildProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument $_ }) -join " ")

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Unable to start '$FilePath'."
    }
    return $process
}

function Wait-ForUnauthenticatedResponse {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$Description
    )

    for ($attempt = 0; $attempt -lt 100; $attempt += 1) {
        if ($Process.HasExited) {
            throw "$Description stopped before it became ready."
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                return
            }
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
    throw "$Description did not provide a ready unauthenticated response at '$Uri'."
}

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" |
        Select-Object -ExpandProperty ProcessId
    foreach ($childId in $children) {
        Stop-ProcessTree -ProcessId $childId
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

try {
    if ($DesktopPort -eq 8000) {
        throw "DesktopPort must not be 8000 because the local platform uses 127.0.0.1:8000."
    }
    Assert-RequiredFile -Path $desktopPython -Description "The desktop virtual environment Python"
    Assert-RequiredFile -Path $backendPython -Description "The backend virtual environment Python"
    Assert-RequiredFile -Path (Join-Path $backendProject "manage.py") -Description "The backend manage.py"

    $backendProcess = Start-ChildProcess `
        -FilePath $backendPython `
        -Arguments @("manage.py", "runserver", "127.0.0.1:8000", "--noreload") `
        -WorkingDirectory $backendProject
    Wait-ForUnauthenticatedResponse `
        -Uri "http://127.0.0.1:8000/admin/login/" `
        -Process $backendProcess `
        -Description "The local platform"

    $desktopProcess = Start-ChildProcess `
        -FilePath $desktopPython `
        -Arguments @("-m", "broccoli_desktop.browser_only", "--port", "$DesktopPort", "--local") `
        -WorkingDirectory $desktopRoot
    Wait-ForUnauthenticatedResponse `
        -Uri "http://127.0.0.1:$DesktopPort/health" `
        -Process $desktopProcess `
        -Description "The desktop browser-only service"

    # The desktop child inherits this console, so the launch URL it prints on
    # startup -- the only place the per-launch capability token is exposed --
    # has already appeared above this line. Say so, rather than leaving an
    # operator to wonder which of the two URLs on screen to open.
    Write-Output "Local platform and desktop browser-only service are ready."
    Write-Output "Open the http://127.0.0.1:$DesktopPort/?k=... URL printed above; the bare address carries no launch key."
    Write-Output "Press Ctrl+C to stop both process trees."
    while ($true) {
        if ($backendProcess.HasExited -or $desktopProcess.HasExited) {
            throw "A live integration child process exited unexpectedly."
        }
        Start-Sleep -Seconds 1
    }
} finally {
    if ($null -ne $desktopProcess -and -not $desktopProcess.HasExited) {
        Stop-ProcessTree -ProcessId $desktopProcess.Id
    }
    if ($null -ne $backendProcess -and -not $backendProcess.HasExited) {
        Stop-ProcessTree -ProcessId $backendProcess.Id
    }
}
