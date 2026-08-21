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

function Get-PaintedHistogramPixels {
    return [int](Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const canvas = document.getElementById('microphoneHistogram'); const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data; let painted = 0; for (let index = 3; index < pixels.length; index += 4) { if (pixels[index] > 0) painted += 1; } return painted; })()"
    ) | ConvertFrom-Json)
}

function Get-SessionRowCount {
    return [int](Invoke-Browser -BrowserArguments @(
        "eval",
        "document.querySelectorAll('#sessionLibrary .session-row-content').length"
    ) | ConvertFrom-Json)
}

function Assert-TranscriptClearsTheDock {
    $clearance = (Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const rows = document.querySelectorAll('#transcriptTimeline article'); if (!rows.length) return null; const last = rows[rows.length - 1].getBoundingClientRect(); const dock = document.getElementById('captureDock').getBoundingClientRect(); return Math.round(dock.top - last.bottom); })()"
    ) | ConvertFrom-Json)
    if ($null -eq $clearance) {
        throw "No transcript row was on screen to measure against the capture panel."
    }
    if ($clearance -lt 0) {
        throw "The last transcript row is $([Math]::Abs($clearance))px behind the capture panel."
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
    if ($LASTEXITCODE -ne 0) { throw "agent-browser is not available." }
    $parsed = [Version](($version -replace '^agent-browser\s+', '') -replace '-.*$', '')
    if ($parsed -lt [Version]"0.34.0") {
        throw "Expected agent-browser 0.34.0 or newer, received '$version'."
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
    # Matches tests/fakes.py visual_history(): SESSION_PAGE_SIZE * 2 + 5.
    $seededSessionCount = 45
    $finalSegmentText = "we should ship"

    Invoke-Browser -BrowserArguments @("open", "http://127.0.0.1:8765/?k=visual-capability-token")
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
    $focused = (Invoke-Browser -BrowserArguments @(
        "eval",
        "document.activeElement.tagName + '#' + document.activeElement.id"
    ) | ConvertFrom-Json)
    if ($focused -eq "BODY#") {
        throw "Focus was left on the body after entering the capture screen."
    }
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-ready.png"))

    # The controls that were removed must stay removed.
    $removed = Invoke-Browser -BrowserArguments @(
        "eval",
        "[!!document.querySelector('[data-testid=open-broccoli]'), !!document.querySelector('[data-testid=load-more]'), !!document.getElementById('themeToggleButton')]"
    ) | ConvertFrom-Json
    if ($removed -contains $true) {
        throw "A control that should have been removed is still in the shell: $($removed -join ',')"
    }

    # Newest first. The seeded history mixes timestamps with and without
    # fractional seconds, which is the pair the old text comparison inverted.
    Invoke-Browser -BrowserArguments @("wait", "--text", "Reuni$([char]0x00E3)o arquivada 44")
    $firstRow = (Invoke-Browser -BrowserArguments @(
        "eval",
        "document.querySelector('#sessionLibrary .session-row-content').getAttribute('aria-label')"
    ) | ConvertFrom-Json)
    if ($firstRow -notlike "*44") {
        throw "The newest session is not at the top of the list: $firstRow"
    }

    # Opening a session from the sidebar is the case the reviewer traced the
    # bug to: the list used to be rebuilt from scratch on selection, which tore
    # out whatever row -- or its "..." menu trigger -- held focus. Clicked here,
    # before the list has anything to scroll, so the row is on screen without
    # relying on the infinite-scroll behaviour the next block exercises.
    Invoke-Browser -BrowserArguments @("find", "testid", "session-row-history-44", "click")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#sessionTitle').value === 'Reuni$([char]0x00E3)o arquivada 44'"
    )
    $focused = (Invoke-Browser -BrowserArguments @(
        "eval",
        "document.activeElement.tagName + '#' + document.activeElement.id"
    ) | ConvertFrom-Json)
    if ($focused -eq "BODY#") {
        throw "Focus was left on the body after opening a session from the sidebar."
    }
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    # Infinite scroll. The list is three pages deep and nothing here clicks:
    # reaching the end is the whole trigger.
    $rows = Get-SessionRowCount
    if ($rows -ge $seededSessionCount) {
        throw "The history arrived whole, so scrolling cannot be what loads it."
    }
    for ($attempt = 0; $attempt -lt 8 -and $rows -lt $seededSessionCount; $attempt += 1) {
        $previous = $rows
        Invoke-Browser -BrowserArguments @(
            "eval",
            "(() => { const box = document.querySelector('.conversations-section-body'); box.scrollTop = box.scrollHeight; })()"
        ) | Out-Null
        Invoke-Browser -BrowserArguments @(
            "wait",
            "--fn",
            "document.querySelectorAll('#sessionLibrary .session-row-content').length > $previous"
        )
        $rows = Get-SessionRowCount
    }
    if ($rows -ne $seededSessionCount) {
        throw "Infinite scroll stopped at $rows of $seededSessionCount sessions."
    }
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.getElementById('sessionsSentinel').classList.contains('hidden')"
    )
    # The search box shares the scroll container with the list, so a history
    # long enough to scroll is exactly what used to carry it out of view.
    $searchOffset = (Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const box = document.querySelector('.conversations-section-body').getBoundingClientRect(); const search = document.getElementById('sessionSearchInput').getBoundingClientRect(); return Math.round(search.top - box.top); })()"
    ) | ConvertFrom-Json)
    if ($searchOffset -gt 8) {
        throw "The session search scrolled $searchOffset px away with the list."
    }
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "history-scrolled.png"))

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
    Invoke-Browser -BrowserArguments @("eval", "document.getElementById('themeLightOption').click()") | Out-Null
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.documentElement.getAttribute('data-theme') === 'light'"
    )
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "settings-light.png"))
    Assert-NoAccessibilityViolations -Screen "the light settings screen"
    Invoke-Browser -BrowserArguments @("eval", "document.getElementById('themeDarkOption').click()") | Out-Null
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.documentElement.getAttribute('data-theme') === 'dark'"
    )
    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Voltar para a captura")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Transcri$([char]0x00E7)$([char]0x00E3)o")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    Invoke-Browser -BrowserArguments @("find", "testid", "new-session", "click")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#sessionTitle').value.trim().length > 0"
    )
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

    # Scrolling up must not be interrupted, and must not raise the jump control
    # on its own either: it is gated on content arriving behind the reader, not
    # on the scroll position alone. (The other half of the rule -- the control
    # appearing when a segment lands while scrolled up -- needs a stream that
    # keeps producing, which this fake does not.)
    $jumpVisibleWhileIdle = (Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const log = document.getElementById('transcriptTimeline'); log.scrollTop = 0; log.dispatchEvent(new Event('scroll')); return !document.getElementById('jumpToLatestButton').classList.contains('hidden'); })()"
    ) | ConvertFrom-Json)
    if ($jumpVisibleWhileIdle) {
        throw "The jump control appeared with no new transcript content behind it."
    }

    # The histogram is a canvas now, so "did it render" is a question about
    # pixels: a zero here means the size, the context or the theme colour the
    # renderer reads back from the stylesheet went missing.
    if ((Get-PaintedHistogramPixels) -le 0) {
        throw "The capture histogram drew nothing while streaming."
    }

    # Reduced motion must not mean a dead histogram -- it means no frame loop.
    Invoke-Browser -BrowserArguments @("set", "media", "dark", "reduced-motion")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    if ((Get-PaintedHistogramPixels) -le 0) {
        throw "The capture histogram went blank under reduced motion."
    }
    Invoke-Browser -BrowserArguments @("set", "media", "dark")

    Assert-TranscriptClearsTheDock

    Invoke-Browser -BrowserArguments @("set", "viewport", "375", "812")
    Assert-TranscriptClearsTheDock
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-narrow.png"))
    Invoke-Browser -BrowserArguments @("set", "viewport", "1440", "900")

    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Parar captura")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Captura encerrada.")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    # Renaming from the header reaches both the header and the sidebar row.
    Invoke-Browser -BrowserArguments @("find", "testid", "rename-session", "click")
    Invoke-Browser -BrowserArguments @("find", "testid", "rename-session-input", "fill", "Sess$([char]0x00E3)o renomeada")
    Invoke-Browser -BrowserArguments @("find", "testid", "rename-session-confirm", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Sess$([char]0x00E3)o renomeada.")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#sessionTitle').value === 'Sess$([char]0x00E3)o renomeada'"
    )
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
    # A bare reload would hit the URL app.js already stripped the launch key
    # from, and the loopback API refuses every /api/* request without it --
    # reopening the same launch URL is what a real relaunch would do instead.
    Invoke-Browser -BrowserArguments @("open", "http://127.0.0.1:8765/?k=visual-capability-token")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Transcri$([char]0x00E7)$([char]0x00E3)o")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-stopped-dark.png"))
    $errorsJson = Invoke-Browser -BrowserArguments @("errors", "--json")
    $errors = @(($errorsJson | ConvertFrom-Json).data.errors)
    if (@($errors).Count -ne 0) {
        throw "Browser console errors were reported: $errorsJson"
    }
    Assert-NoAccessibilityViolations -Screen "the dark capture screen"
    $storage = Invoke-Browser -BrowserArguments @("eval", "[document.documentElement.innerText.includes('visual-test-token'), localStorage.getItem('broccoli-desktop-settings')?.includes('visual-test-token') ?? false, sessionStorage.length, document.documentElement.innerText.includes('visual-capability-token')]") | ConvertFrom-Json
    if (@($storage).Count -ne 4 -or $storage[0] -ne $false -or $storage[1] -ne $false -or $storage[2] -ne 0 -or $storage[3] -ne $false) {
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
