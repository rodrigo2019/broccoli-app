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

    # Heading structure (each screen has exactly one <h1>, in the right order
    # relative to its sub-headings) is covered by axe's best-practice tag, not
    # wcag2a/wcag2aa -- run it separately and check specifically for the two
    # rules the heading-structure fix targets, rather than failing on any
    # best-practice violation, since that tag also covers things out of scope
    # here (e.g. region, landmark-one-main).
    $bestPracticeJson = Invoke-Browser -BrowserArguments @("a11y", "--tags", "best-practice", "--json")
    $bestPracticeViolations = @((($bestPracticeJson | ConvertFrom-Json).data).violations)
    $headingViolations = @($bestPracticeViolations | Where-Object { $_.id -in @("page-has-heading-one", "heading-order") })
    if ($headingViolations.Count -ne 0) {
        throw "Heading structure violations were reported on $Screen`: $($headingViolations | ConvertTo-Json -Depth 6)"
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

function Assert-NoLeakedSecret {
    param(
        [Parameter(Mandatory = $true)][string]$Secret,
        [Parameter(Mandatory = $true)][string]$Context
    )

    # Scans every localStorage key, not a named one -- the secret must never
    # reach *any* key, including one reintroduced under a different name.
    $check = "(() => { const keys = Object.keys(localStorage); const inStorage = keys.some((key) => (localStorage.getItem(key) || '').includes('$Secret')); return [document.documentElement.innerText.includes('$Secret'), inStorage]; })()"
    $result = Invoke-Browser -BrowserArguments @("eval", $check) | ConvertFrom-Json
    if (@($result).Count -ne 2 -or $result[0] -ne $false -or $result[1] -ne $false) {
        throw "A fake secret leaked into page text or localStorage ($Context)."
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
    # The login screen never had an axe scan, so the token field's accessible
    # name -- the whole point of the fieldset/legend/aria-labelledby work --
    # had no regression guard beyond string assertions, which a typo breaking
    # the id pairing while keeping both substrings would pass.
    Assert-NoAccessibilityViolations -Screen "the login screen"

    # /api/settings is deliberately unauthenticated because a corporate proxy
    # can be what stands between this window and the login request itself. That
    # is only true if the panel opens before signing in, which it did not: the
    # sidebar entry point lives in a footer hidden until authentication, and
    # showSettings returned early.
    Invoke-Browser -BrowserArguments @("find", "testid", "network-settings", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Conex$([char]0x00E3)o")
    $preLoginSettings = Invoke-Browser -BrowserArguments @(
        "eval",
        "['settingsView', 'settingsDevicesSection', 'settingsAppearanceSection'].map((id) => document.getElementById(id).classList.contains('hidden'))"
    ) | ConvertFrom-Json
    $preLoginSettings = @($preLoginSettings)
    if ($preLoginSettings[0] -ne $false) {
        throw "The settings screen did not open before login."
    }
    if ($preLoginSettings[1] -ne $true -or $preLoginSettings[2] -ne $true) {
        throw "The pre-login settings screen showed sections that need an authenticated session."
    }
    # Expanded, so the four proxy fields are actually in the tree the scan
    # walks -- the post-login settings scan below runs while #proxyFields is
    # collapsed and structurally cannot see them.
    Invoke-Browser -BrowserArguments @("find", "testid", "proxy-toggle", "click")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Assert-NoAccessibilityViolations -Screen "the pre-login network settings screen"
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "settings-pre-login.png"))
    Invoke-Browser -BrowserArguments @("find", "testid", "proxy-toggle", "click")
    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Voltar para o login")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Entrar")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

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

    # The skip link is intentionally off-screen (`transform: translateY(-120%)`)
    # except while focused, so `find ... click` -- which expects an actionable,
    # in-viewport target -- is the wrong tool here; a synthetic click exercises
    # the same click handler a real activation (mouse or Enter-on-link) fires.
    # Focus starts outside #mainView on purpose, so landing inside it proves
    # the skip link moved focus rather than merely finding it already there.
    Invoke-Browser -BrowserArguments @("eval", "document.getElementById('sessionSearchInput').focus()") | Out-Null
    Invoke-Browser -BrowserArguments @("eval", "document.querySelector('[data-testid=skip-to-main]').click()") | Out-Null
    $skipLanded = (Invoke-Browser -BrowserArguments @(
        "eval",
        "document.getElementById('mainView').contains(document.activeElement)"
    ) | ConvertFrom-Json)
    if ($skipLanded -ne $true) {
        throw "Activating the skip link did not move focus into #mainView."
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

    # Pinned first, then newest. The seeded history mixes timestamps with and
    # without fractional seconds, which is the pair the old text comparison
    # inverted, and pins one older-day session (history-02) so a pin sorting
    # ahead of a newer, unpinned session is exercised from the very first
    # page, not just after the fact.
    Invoke-Browser -BrowserArguments @("wait", "--text", "Reuni$([char]0x00E3)o arquivada 44")
    $topTestIds = Invoke-Browser -BrowserArguments @(
        "eval",
        "Array.from(document.querySelectorAll('#sessionLibrary .session-row-content')).slice(0, 2).map((el) => el.dataset.testid)"
    ) | ConvertFrom-Json
    $topTestIds = @($topTestIds)
    if ($topTestIds[0] -ne "session-row-history-02") {
        throw "The pinned session is not at the top of the list: $($topTestIds -join ', ')"
    }
    if ($topTestIds[1] -ne "session-row-history-44") {
        throw "The newest unpinned session is not first after the pinned section: $($topTestIds -join ', ')"
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

    # Segregating pinned rows into their own "Fixadas" section, ahead of the
    # day-grouped list, is what stops a single calendar day from rendering as
    # two separate, non-adjacent headings: history-02 is pinned to a day
    # (2026-08-18) that history-00/01 also share, unpinned, further down.
    # Filtered to the `YYYY-MM-DD` shape so the "Fixadas" heading itself --
    # a real heading, but not a date -- is never counted as a repeat.
    $dayHeadings = Invoke-Browser -BrowserArguments @(
        "eval",
        "Array.from(document.querySelectorAll('#sessionLibrary li[data-group-key]')).map((el) => el.dataset.groupKey).filter((key) => /^\d{4}-\d{2}-\d{2}$/.test(key))"
    ) | ConvertFrom-Json
    $dayHeadings = @($dayHeadings)
    # A fixture with only one day (or none) can never fail the checks below --
    # this is the guard against the gap the fixture used to leave open.
    if ($dayHeadings.Count -lt 2) {
        throw "Expected at least two distinct day headings in the seeded history, found $($dayHeadings.Count): $($dayHeadings -join ', ')"
    }
    $uniqueHeadings = @($dayHeadings | Select-Object -Unique)
    if ($uniqueHeadings.Count -ne $dayHeadings.Count) {
        throw "Day headings repeat: $($dayHeadings -join ', ')"
    }
    $descendingHeadings = @($dayHeadings | Sort-Object -Descending)
    for ($index = 0; $index -lt $dayHeadings.Count; $index += 1) {
        if ($dayHeadings[$index] -ne $descendingHeadings[$index]) {
            throw "Day headings are not in descending order: $($dayHeadings -join ', ')"
        }
    }

    # Reconciliation treats a row that arrived via infinite scroll the same as
    # one from the initial page, but the suite should still exercise that path
    # rather than only ever clicking a row that was there from the start.
    # Scrolled fully into view first (`block: center`), not just "the list is
    # scrolled somewhere" -- a target hugging the viewport edge is what made
    # the click on this exact row unreliable earlier, not the app.
    Invoke-Browser -BrowserArguments @(
        "eval",
        "document.querySelector('[data-testid=session-row-history-00]').scrollIntoView({ block: 'center' })"
    ) | Out-Null
    Invoke-Browser -BrowserArguments @("find", "testid", "session-row-history-00", "click")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#sessionTitle').value === 'Reuni$([char]0x00E3)o arquivada 00'"
    )
    $focused = (Invoke-Browser -BrowserArguments @(
        "eval",
        "document.activeElement.tagName + '#' + document.activeElement.id"
    ) | ConvertFrom-Json)
    if ($focused -eq "BODY#") {
        throw "Focus was left on the body after opening a paginated sidebar session."
    }
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

    # The proxy panel used to persist its whole state -- password included, in
    # clear text -- to localStorage. Exercise it here so the storage assertion
    # at the end of this script has a real fake secret to look for.
    $fakeProxyPassword = "visual-test-proxy-secret"
    Invoke-Browser -BrowserArguments @("find", "testid", "proxy-toggle", "click")
    Invoke-Browser -BrowserArguments @("eval", "document.getElementById('proxyHost').value = 'proxy.visual-check.invalid'") | Out-Null
    Invoke-Browser -BrowserArguments @("eval", "document.getElementById('proxyPort').value = '8080'") | Out-Null
    Invoke-Browser -BrowserArguments @("eval", "document.getElementById('proxyUsername').value = 'visual-user'") | Out-Null
    Invoke-Browser -BrowserArguments @("find", "testid", "proxy-password", "fill", $fakeProxyPassword)
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    # The settings scan above ran with #proxyFields still collapsed, so it could
    # not see these four fields at all -- the gate structurally could not catch
    # a wrong accessible name on any of them. This one can.
    Assert-NoAccessibilityViolations -Screen "the settings screen with the proxy panel expanded"
    Invoke-Browser -BrowserArguments @("find", "testid", "test-proxy", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Conex$([char]0x00E3)o bem-sucedida.")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "settings-proxy-dark.png"))
    Invoke-Browser -BrowserArguments @("find", "testid", "save-settings", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Configura$([char]0x00E7)$([char]0x00F5)es salvas nesta m$([char]0x00E1)quina.")
    # The saved password must never come back to the page: the field has to be
    # blank again once the save round trip (POST, then the re-fetch that
    # repopulates the form) has completed.
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.getElementById('proxyPassword').value === ''"
    )
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    # Checked here, on the settings screen, straight after the save round trip
    # -- not only after the later reload. app.js scrubs the panel's legacy
    # localStorage key on every load, which would erase the evidence of a
    # reintroduced persistSettings() write moments before a post-reload-only
    # check ran. This is the check that actually catches that regression.
    Assert-NoLeakedSecret -Secret $fakeProxyPassword -Context "immediately after saving the proxy, before any reload"

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
    Invoke-Browser -BrowserArguments @("wait", "--text", "O t$([char]0x00ED)tulo da reuni$([char]0x00E3)o n$([char]0x00E3)o $([char]0x00E9) v$([char]0x00E1)lido.")
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
    # Pins the headline finding: the session title used to collapse to 2px
    # wide at this width, with the session code and segment count crowding it
    # out of the header. 120px is well under the 128px floor the title now
    # holds to, leaving margin for future header changes without making this
    # brittle.
    $titleWidth = (Invoke-Browser -BrowserArguments @(
        "eval",
        "Math.round(document.querySelector('#sessionTitle').getBoundingClientRect().width)"
    ) | ConvertFrom-Json)
    if ($titleWidth -lt 120) {
        throw "The session title collapsed to ${titleWidth}px at 375px wide."
    }
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
    # Seeded here, right before the reload below, so the scrub app.js runs on
    # every load is covered by an assertion of its own instead of silently
    # covering for the checks around the settings save above: a value under
    # the panel's old key must survive up to the reload and be gone after it.
    Invoke-Browser -BrowserArguments @(
        "eval",
        "localStorage.setItem('broccoli-desktop-settings', JSON.stringify({ proxyPassword: 'legacy-scrub-sentinel' }))"
    ) | Out-Null
    # Deliberately the stripped URL, with no key on it: F5/Ctrl-R is enabled by
    # default in WebView2, a renderer crash reloads, and the context menu offers
    # it. This used to brick the window -- the shell still rendered, every
    # /api/* answered 403 and the event socket closed with 1008, with nothing on
    # screen saying so. Reaching the capture screen here is the whole test: it
    # means the bootstrap arrived, which means the key was recovered.
    Invoke-Browser -BrowserArguments @("open", "http://127.0.0.1:8765/")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Transcri$([char]0x00E7)$([char]0x00E3)o")
    $reloadedUrl = Invoke-Browser -BrowserArguments @("eval", "location.href") | ConvertFrom-Json
    if ($reloadedUrl -match "[?&]k=") {
        throw "The reload under test carried a launch key in the URL, so it proved nothing: $reloadedUrl"
    }
    $legacyKeyRemaining = (Invoke-Browser -BrowserArguments @(
        "eval",
        "localStorage.getItem('broccoli-desktop-settings')"
    ) | ConvertFrom-Json)
    if ($null -ne $legacyKeyRemaining) {
        throw "The legacy broccoli-desktop-settings localStorage key survived a reload instead of being scrubbed."
    }
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-stopped-dark.png"))
    $errorsJson = Invoke-Browser -BrowserArguments @("errors", "--json")
    $errors = @(($errorsJson | ConvertFrom-Json).data.errors)
    if (@($errors).Count -ne 0) {
        throw "Browser console errors were reported: $errorsJson"
    }
    Assert-NoAccessibilityViolations -Screen "the dark capture screen"
    # Scans every localStorage key rather than the one the settings panel used
    # to write to -- the proxy password must never reach *any* key, and this
    # also has to catch the panel's own legacy key if an old value from before
    # this feature ever got scrubbed on load.
    # sessionStorage is no longer required to be empty: the launch key lives
    # there for the life of the window, which is what makes a reload survivable.
    # It is required to hold that one key and nothing else, and neither storage
    # may ever carry the access token or the proxy password.
    $storageCheck = "(() => { const secrets = ['visual-test-token', '$fakeProxyPassword']; const inStore = (store, needle) => Object.keys(store).some((key) => (store.getItem(key) || '').includes(needle)); return [document.documentElement.innerText.includes('visual-test-token'), inStore(localStorage, 'visual-test-token'), Object.keys(sessionStorage).join(','), document.documentElement.innerText.includes('visual-capability-token'), document.documentElement.innerText.includes('$fakeProxyPassword'), inStore(localStorage, '$fakeProxyPassword'), secrets.some((needle) => inStore(sessionStorage, needle))]; })()"
    $storage = Invoke-Browser -BrowserArguments @("eval", $storageCheck) | ConvertFrom-Json
    if (@($storage).Count -ne 7 -or $storage[0] -ne $false -or $storage[1] -ne $false -or $storage[3] -ne $false -or $storage[4] -ne $false -or $storage[5] -ne $false -or $storage[6] -ne $false) {
        throw "The browser retained fake credential data in page text or web storage."
    }
    if ($storage[2] -ne "broccoli-desktop-launch-key") {
        throw "sessionStorage held something other than the launch key alone: '$($storage[2])'."
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
