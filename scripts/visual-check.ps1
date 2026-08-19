[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$artifactDirectory = Join-Path $projectRoot "artifacts\visual"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$session = $null
$server = $null
$passed = $false

function Invoke-Browser {
    param([Parameter(Mandatory = $true)][string[]]$BrowserArguments)

    & agent-browser @browser @BrowserArguments
    if ($LASTEXITCODE -ne 0) {
        throw "agent-browser command failed: $($BrowserArguments -join ' ')"
    }
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
    $version = (& agent-browser --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -ne "agent-browser 0.34.0") {
        throw "Expected agent-browser 0.34.0, received '$version'."
    }

    New-Item -ItemType Directory -Force -Path $artifactDirectory | Out-Null
    $server = Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "tests.visual_server", "--port", "8765") `
        -WindowStyle Hidden `
        -PassThru

    $healthy = $false
    for ($attempt = 0; $attempt -lt 100; $attempt += 1) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8765/health" -TimeoutSec 1
            if ($health.StatusCode -eq 200) {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
    if (-not $healthy) {
        throw "The fake browser-only server did not become healthy."
    }

    $session = (& agent-browser session id --scope worktree --prefix broccoli-desktop-visual).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($session)) {
        throw "Unable to create the isolated agent-browser session."
    }
    $browser = @("--session", $session, "--allowed-domains", "127.0.0.1,localhost")
    $invalidTokenText = "Token inv$([char]0x00E1)lido"
    $newSessionText = "Nova sess$([char]0x00E3)o"
    $copiedCodeText = "C$([char]0x00F3)digo copiado"
    $meetingNotebookText = "Caderno de reuni$([char]0x00E3)o"

    Invoke-Browser -BrowserArguments @("open", "http://127.0.0.1:8765/")
    Invoke-Browser -BrowserArguments @("set", "viewport", "1440", "900")
    Invoke-Browser -BrowserArguments @("set", "media", "light")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Entrar")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "login.png"))
    Invoke-Browser -BrowserArguments @("find", "testid", "token-input", "fill", "bad-token")
    Invoke-Browser -BrowserArguments @("find", "testid", "login-submit", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", $invalidTokenText)
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("find", "testid", "token-input", "fill", "visual-test-token")
    Invoke-Browser -BrowserArguments @("find", "testid", "login-submit", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", $newSessionText)
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "notebook-light.png"))
    Invoke-Browser -BrowserArguments @("find", "testid", "session-search", "fill", "Daily")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Daily")
    Invoke-Browser -BrowserArguments @("find", "testid", "session-row-session-1", "click")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("find", "testid", "resume-session", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", "we should ship")
    Invoke-Browser -BrowserArguments @("find", "testid", "copy-session-code", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", $copiedCodeText)
    $openBroccoliUrl = (Invoke-Browser -BrowserArguments @("get", "attr", '[data-testid="open-broccoli"]', "href") | Select-Object -Last 1).Trim()
    if ($openBroccoliUrl -ne "http://127.0.0.1:8000") {
        throw "Unexpected fake bootstrap URL: '$openBroccoliUrl'."
    }
    Invoke-Browser -BrowserArguments @("wait", "--text", "Transmitindo")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "notebook-streaming.png"))
    Invoke-Browser -BrowserArguments @("set", "media", "dark")
    Invoke-Browser -BrowserArguments @("reload")
    Invoke-Browser -BrowserArguments @("wait", "--text", $meetingNotebookText)
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "notebook-dark.png"))
    Invoke-Browser -BrowserArguments @("a11y", "--tags", "wcag2a,wcag2aa")

    $errorsJson = Invoke-Browser -BrowserArguments @("errors", "--json")
    $errors = @(($errorsJson | ConvertFrom-Json).data.errors)
    if (@($errors).Count -ne 0) {
        throw "Browser console errors were reported: $errorsJson"
    }
    $accessibilityJson = Invoke-Browser -BrowserArguments @("a11y", "--tags", "wcag2a,wcag2aa", "--json")
    $accessibility = ($accessibilityJson | ConvertFrom-Json).data
    if (@($accessibility.violations).Count -ne 0) {
        throw "Accessibility violations were reported: $accessibilityJson"
    }
    $storage = Invoke-Browser -BrowserArguments @("eval", "[document.documentElement.innerText.includes('visual-test-token'), localStorage.length, sessionStorage.length]") | ConvertFrom-Json
    if (@($storage).Count -ne 3 -or $storage[0] -ne $false -or $storage[1] -ne 0 -or $storage[2] -ne 0) {
        throw "The browser retained fake credential data in page text or web storage."
    }
    $passed = $true
} finally {
    if ($null -ne $session) {
        & agent-browser --session $session --allowed-domains "127.0.0.1,localhost" close | Out-Null
    }
    if ($null -ne $server -and -not $server.HasExited) {
        Stop-ProcessTree -ProcessId $server.Id
    }
}

if ($passed) {
    Write-Output "Visual QA passed."
}
