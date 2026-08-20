[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$artifactDirectory = Join-Path $projectRoot "artifacts\visual"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$session = "broccoli-desktop-visual-qa"
$namespace = "broccoli-desktop-visual-$PID"
$server = $null
$passed = $false

function Invoke-Browser {
    param([Parameter(Mandatory = $true)][string[]]$BrowserArguments)

    & agent-browser @browser @BrowserArguments
    if ($LASTEXITCODE -ne 0) {
        throw "agent-browser command failed: $($BrowserArguments -join ' ')"
    }
}

function Assert-NoAccessibilityViolations {
    param([Parameter(Mandatory = $true)][string]$Screen)

    $json = Invoke-Browser -BrowserArguments @("a11y", "--tags", "wcag2a,wcag2aa", "--json")
    $violations = @((($json | ConvertFrom-Json).data).violations)
    if ($violations.Count -ne 0) {
        throw "Accessibility violations were reported on $Screen`: $json"
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

    # This fake-only session is headed, ephemeral, and limited to the two loopback names.
    $browser = @("--session", $session, "--namespace", $namespace, "--headed", "--allowed-domains", "127.0.0.1,localhost")
    $invalidTokenText = "Token inv$([char]0x00E1)lido"
    $finalSegmentText = "we should ship"

    Invoke-Browser -BrowserArguments @("open", "http://127.0.0.1:8765/")
    Invoke-Browser -BrowserArguments @("set", "viewport", "1440", "900")
    Invoke-Browser -BrowserArguments @("set", "media", "light")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Entrar")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "login.png"))

    Invoke-Browser -BrowserArguments @("find", "testid", "token-input", "fill", "bad-token")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("find", "testid", "login-submit", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", $invalidTokenText)
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    Invoke-Browser -BrowserArguments @("find", "testid", "token-input", "fill", "visual-test-token")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("find", "testid", "login-submit", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Transcri$([char]0x00E7)$([char]0x00E3)o")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-ready.png"))

    Invoke-Browser -BrowserArguments @("find", "testid", "settings-button", "click")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("select", "#settingsMicrophoneSelect", "mic-1")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("select", "#settingsSystemDeviceSelect", "system-1")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("find", "testid", "save-settings", "click")
    # Settings is where every field lives, so it is where a regression in the
    # DaisyUI 5 fieldset idiom would show first -- capture it in both themes.
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "settings-dark.png"))
    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Alternar tema")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.documentElement.getAttribute('data-theme') === 'light'"
    )
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "settings-light.png"))
    Assert-NoAccessibilityViolations -Screen "the light settings screen"
    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Alternar tema")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.documentElement.getAttribute('data-theme') === 'dark'"
    )
    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Voltar para a captura")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Transcri$([char]0x00E7)$([char]0x00E3)o")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    $overlongTitle = "x" * 121
    Invoke-Browser -BrowserArguments @("eval", "document.querySelector('#sessionTitle').value = '$overlongTitle'")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Iniciar captura")
    Invoke-Browser -BrowserArguments @("wait", "--text", "The session title is invalid.")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    $startEnabled = (Invoke-Browser -BrowserArguments @("is", "enabled", "#captureToggleButton") | Select-Object -Last 1).Trim()
    if ($startEnabled -ne "true") {
        throw "The start button remained disabled after the rejected start request."
    }
    Invoke-Browser -BrowserArguments @("eval", "document.querySelector('#sessionTitle').value = ''")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Iniciar captura")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#captureToggleButton')?.getAttribute('aria-label') === 'Parar captura'"
    )
    Invoke-Browser -BrowserArguments @("wait", "--text", $finalSegmentText)
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-streaming.png"))

    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Parar captura")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Captura encerrada.")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    # The sidebar search reaches the backend through the local API; a term that
    # matches nothing has to empty the list rather than leave it stale.
    Invoke-Browser -BrowserArguments @("find", "testid", "session-search", "fill", "zzz-no-match")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Nenhum resultado")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    # Not `fill ""`: PowerShell drops an empty string when binding [string[]].
    # Clearing through the DOM needs the input event too, or the debounce that
    # drives the search never runs.
    Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const box = document.querySelector('#sessionSearchInput'); box.value = ''; box.dispatchEvent(new Event('input', { bubbles: true })); })()"
    )
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelectorAll('#sessionLibrary .session-row-content').length > 0"
    )
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("set", "media", "dark")
    Invoke-Browser -BrowserArguments @("reload")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Transcri$([char]0x00E7)$([char]0x00E3)o")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-stopped-dark.png"))
    $errorsJson = Invoke-Browser -BrowserArguments @("errors", "--json")
    $errors = @(($errorsJson | ConvertFrom-Json).data.errors)
    if (@($errors).Count -ne 0) {
        throw "Browser console errors were reported: $errorsJson"
    }
    Assert-NoAccessibilityViolations -Screen "the dark capture screen"
    $storage = Invoke-Browser -BrowserArguments @("eval", "[document.documentElement.innerText.includes('visual-test-token'), localStorage.getItem('broccoli-desktop-settings')?.includes('visual-test-token') ?? false, sessionStorage.length]") | ConvertFrom-Json
    if (@($storage).Count -ne 3 -or $storage[0] -ne $false -or $storage[1] -ne $false -or $storage[2] -ne 0) {
        throw "The browser retained fake credential data in page text or web storage."
    }
    $passed = $true
} finally {
    & agent-browser --session $session --namespace $namespace --headed --allowed-domains "127.0.0.1,localhost" close | Out-Null
    if ($null -ne $server -and -not $server.HasExited) {
        Stop-ProcessTree -ProcessId $server.Id
    }
}

if ($passed) {
    Write-Output "Visual QA passed."
}
