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

function Get-ChannelLayoutViolation {
    # Worst horizontal violation, in px, across both channel cards: a row
    # member (mute square, titles, language column, or the select itself)
    # spilling past its card's edges, or two row members drawn over each
    # other. Zero-ish when the row is clean -- this is what catches a
    # squeezed dock rendering the language select on top of the channel name.
    return [int](Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { let worst = 0; for (const id of ['microphoneChannel', 'systemChannel']) { const card = document.getElementById(id); const cardBox = card.getBoundingClientRect(); const members = ['.capture-channel__mute', '.capture-channel__titles', '.capture-channel__language-group'].map((cls) => card.querySelector(cls).getBoundingClientRect()); const edges = members.concat([card.querySelector('.capture-channel__language').getBoundingClientRect()]); for (const box of edges) { worst = Math.max(worst, Math.round(cardBox.left - box.left), Math.round(box.right - cardBox.right)); } for (let i = 0; i + 1 < members.length; i += 1) { worst = Math.max(worst, Math.round(members[i].right - members[i + 1].left)); } } return worst; })()"
    ) | ConvertFrom-Json)
}

function Get-SignalBarState {
    param([Parameter(Mandatory = $true)][string]$ChannelId)

    # Computed opacity, not the data-level attribute: a lit bar is one the
    # stylesheet actually painted as lit, so a broken lit rule fails here even
    # while the JS keeps writing the right level.
    $state = Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const bars = Array.from(document.querySelectorAll('#$ChannelId .capture-signal__bar')); const lit = bars.filter((bar) => Number(getComputedStyle(bar).opacity) > 0.85).length; return [bars.length, lit]; })()"
    ) | ConvertFrom-Json
    return @($state)
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
    # --locale, explicitly: every literal this script waits on below is
    # pt-BR, and a fresh install now follows the Windows display language.
    # Without pinning it, this gate would pass or hang depending on the
    # language of the machine it runs on -- and a wait on missing text hangs
    # rather than failing, so it would not even say why.
    $server = Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "tests.visual_server", "--port", "8765", "--locale", "pt-BR") `
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
    # The real pre-login window is COMPACT_WINDOW_SIZE (1100x600, runtime.py)
    # and cannot be dragged to any other size, so the pre-login screens are
    # exercised at exactly that size; the viewport grows to 1440x900 after
    # signing in, where the runtime maximizes the window.
    Invoke-Browser -BrowserArguments @("set", "viewport", "1100", "600")
    Invoke-Browser -BrowserArguments @("set", "media", "light")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Entrar")
    # The login screen is the whole window: the sidebar and topbar render
    # nothing before authentication, so they must not reserve space that
    # pushes the card off the window's center -- or, once the error line is
    # up, past the 600px edge into a scrollbar (asserted after the bad-token
    # attempt below).
    $loginLayout = Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const doc = document.scrollingElement; const card = document.querySelector('#loginView .card').getBoundingClientRect(); return [document.getElementById('appHeader').classList.contains('hidden'), document.getElementById('appSidebar').classList.contains('hidden'), Math.round(Math.abs(card.left + card.width / 2 - innerWidth / 2)), Math.round(Math.abs(card.top + card.height / 2 - innerHeight / 2)), doc.scrollHeight - doc.clientHeight]; })()"
    ) | ConvertFrom-Json
    $loginLayout = @($loginLayout)
    if ($loginLayout[0] -ne $true -or $loginLayout[1] -ne $true) {
        throw "The empty shell chrome is visible on the login screen (header hidden: $($loginLayout[0]), sidebar hidden: $($loginLayout[1]))."
    }
    if ([int]$loginLayout[2] -gt 8 -or [int]$loginLayout[3] -gt 8) {
        throw "The login card sits $($loginLayout[2])px / $($loginLayout[3])px off the compact window's center."
    }
    if ([int]$loginLayout[4] -gt 0) {
        throw "The compact login screen overflows the window by $($loginLayout[4])px."
    }
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
    # focusScreen takes the screen's first heading, and #settingsView's first
    # <h2> is "Dispositivos de áudio" -- inside the section the two checks
    # above just confirmed is hidden. .focus() under a display:none ancestor is
    # a silent no-op, and #loginView is hidden by the same render, so focus
    # fell back to <body>: the next Tab restarted from the top of the document
    # and a screen reader was told nothing had happened. The axe scan below
    # cannot see this -- axe does not check focus management -- so this is the
    # only guard the screen gets.
    $settingsFocus = Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const active = document.activeElement; return [active.tagName + '#' + active.id + '.' + active.className, document.getElementById('settingsView').contains(active), active.getClientRects().length > 0]; })()"
    ) | ConvertFrom-Json
    $settingsFocus = @($settingsFocus)
    if ($settingsFocus[1] -ne $true -or $settingsFocus[2] -ne $true) {
        throw "Opening the pre-login settings screen left focus on $($settingsFocus[0]) instead of a visible element inside it."
    }
    # Expanded, so the four proxy fields are actually in the tree the scan
    # walks -- the post-login settings scan below runs while #proxyFields is
    # collapsed and structurally cannot see them.
    Invoke-Browser -BrowserArguments @("find", "testid", "proxy-toggle", "click")
    # The connection card takes the form's full width instead of half a
    # two-column grid; its fields stay inside it (nowrap option labels used to
    # push the grid tracks past the card's edge); and the address shares its
    # row with the port. The language card, the only other section on this
    # screen, is widened to match by renderView.
    $proxyLayout = Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const card = document.getElementById('settingsConnectionSection'); const cardBox = card.getBoundingClientRect(); const form = document.getElementById('settingsForm').getBoundingClientRect(); let spill = 0; card.querySelectorAll('*').forEach((el) => { spill = Math.max(spill, Math.round(el.getBoundingClientRect().right - cardBox.right)); }); const host = document.getElementById('proxyHost').getBoundingClientRect(); const port = document.getElementById('proxyPort').getBoundingClientRect(); return [Math.round(form.width - cardBox.width), spill, Math.round(Math.abs(host.top - port.top))]; })()"
    ) | ConvertFrom-Json
    $proxyLayout = @($proxyLayout)
    if ([int]$proxyLayout[0] -gt 2) {
        throw "The pre-login connection card is $($proxyLayout[0])px narrower than its form."
    }
    if ([int]$proxyLayout[1] -gt 1) {
        throw "Proxy fields spill $($proxyLayout[1])px past the connection card's edge."
    }
    if ([int]$proxyLayout[2] -gt 2) {
        throw "The proxy address and port fields are not on one row."
    }
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
    # The error line grows the card by one row; the compact window has to
    # absorb that without a scrollbar.
    $errorOverflow = (Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const doc = document.scrollingElement; return doc.scrollHeight - doc.clientHeight; })()"
    ) | ConvertFrom-Json)
    if ([int]$errorOverflow -gt 0) {
        throw "The login error made the compact window scroll by $errorOverflow px."
    }
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    Invoke-Browser -BrowserArguments @("find", "testid", "token-input", "fill", "visual-test-token")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("find", "testid", "login-submit", "click")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Transcri$([char]0x00E7)$([char]0x00E3)o")
    # Signing in maximizes the real window (runtime.py); the rest of the run
    # exercises the signed-in screens at a desktop size.
    Invoke-Browser -BrowserArguments @("set", "viewport", "1440", "900")
    $focused = (Invoke-Browser -BrowserArguments @(
        "eval",
        "document.activeElement.tagName + '#' + document.activeElement.id"
    ) | ConvertFrom-Json)
    if ($focused -eq "BODY#") {
        throw "Focus was left on the body after entering the capture screen."
    }

    # Landing on the capture screen puts you in front of an unnamed draft, and
    # the field used to sit blank until you pressed "Nova sessao" -- a button
    # there is no reason to press when you have only just opened the app. The
    # existing draft-name check further down runs after that click, so it never
    # covered arriving here. Asserted through the real UI because the fill
    # happens in applyBootstrap, where there is nothing for pytest to reach.
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#sessionTitle').value.trim().length > 0"
    )

    # An empty transcript still fills its column, so the placeholder centers
    # in the panel instead of sitting in a one-line strip at the top.
    $unfilledColumn = (Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const surface = document.getElementById('transcriptTimeline'); const wrapper = surface.parentElement; return Math.round(wrapper.getBoundingClientRect().height - surface.getBoundingClientRect().height); })()"
    ) | ConvertFrom-Json)
    if ([int]$unfilledColumn -gt 2) {
        throw "The empty transcript surface leaves $unfilledColumn px of its column unfilled."
    }

    # The screen's label moved into the topbar. The <h1> stayed behind as
    # sr-only so the skip link and focusScreen still have a heading to land on,
    # which is exactly the kind of thing a redesign quietly deletes.
    $headingKind = (Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const h = document.querySelector('#mainView h1'); return h ? (h.classList.contains('sr-only') && h.getClientRects().length > 0 ? 'sr-only-laid-out' : 'visible') : 'missing'; })()"
    ) | ConvertFrom-Json)
    if ($headingKind -ne "sr-only-laid-out") {
        throw "The capture screen's heading is '$headingKind'; focusScreen and the skip link need an sr-only h1 that is still laid out."
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

    # The dock is one line now: inside each channel the mute square and the
    # language column share a row -- the stacked layout is what made the dock
    # tower over the transcript. Centers, not tops: the pieces have different
    # heights.
    $dockRows = Invoke-Browser -BrowserArguments @(
        "eval",
        "['microphoneChannel', 'systemChannel'].map((id) => { const channel = document.getElementById(id); const mute = channel.querySelector('.capture-channel__mute').getBoundingClientRect(); const language = channel.querySelector('.capture-channel__language-group').getBoundingClientRect(); return Math.round(Math.abs((mute.top + mute.height / 2) - (language.top + language.height / 2))); })"
    ) | ConvertFrom-Json
    $dockRows = @($dockRows)
    if ([int]$dockRows[0] -gt 4 -or [int]$dockRows[1] -gt 4) {
        throw "A capture channel stacks instead of laying out on one row (mute/language center offsets: $($dockRows[0])px, $($dockRows[1])px)."
    }

    $layoutViolation = Get-ChannelLayoutViolation
    if ($layoutViolation -gt 1) {
        throw "Channel card contents overlap or spill by ${layoutViolation}px at 1440px wide."
    }

    # Idle: each channel shows its five signal bars, none lit -- the meter
    # only answers to a running capture.
    foreach ($channelId in @("microphoneChannel", "systemChannel")) {
        $signal = Get-SignalBarState -ChannelId $channelId
        if ([int]$signal[0] -ne 5) {
            throw "Expected five signal bars in $channelId, found $($signal[0])."
        }
        if ([int]$signal[1] -ne 0) {
            throw "$($signal[1]) signal bar(s) lit in $channelId while no capture is running."
        }
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
    # The audio-level cards are styled by the ruleset that matches today's
    # markup: titles in normal case (a stale duplicate ruleset used to
    # uppercase the whole header) and every meter segment on one baseline
    # (the same duplicate hard-coded 18 grid columns and wrapped the 28
    # segments onto a second row). The settings screen also keeps the
    # document as its only scroller.
    $audioCards = Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const subtitle = document.querySelector('.audio-level-card__subtitle'); const segs = Array.from(document.querySelectorAll('#settingsMicrophoneMeter .audio-level-card__segment')); const bottoms = new Set(segs.map((s) => Math.round(s.getBoundingClientRect().bottom))); return [getComputedStyle(subtitle).textTransform, segs.length, bottoms.size, getComputedStyle(document.getElementById('settingsView')).overflowY]; })()"
    ) | ConvertFrom-Json
    $audioCards = @($audioCards)
    if ($audioCards[0] -ne "none") {
        throw "The audio card subtitle inherits text-transform '$($audioCards[0])'."
    }
    if ([int]$audioCards[1] -lt 1 -or [int]$audioCards[2] -ne 1) {
        throw "The microphone meter wrapped: $($audioCards[1]) segments across $($audioCards[2]) baselines."
    }
    if ($audioCards[3] -ne "visible") {
        throw "The settings screen grew a scroller of its own (overflow-y: $($audioCards[3]))."
    }
    # The interface language, end to end: German and back. The round trip is
    # what proves it, not the catalogs -- the window has to ask the service,
    # adopt the answer and repaint without reloading, because the notification
    # area and the Windows dialogs read the same stored value.
    Invoke-Browser -BrowserArguments @("select", "#settingsLocaleSelect", "de")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Verbindung")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.documentElement.lang === 'de'"
    )
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "settings-german.png"))
    Assert-NoAccessibilityViolations -Screen "the settings screen in German"
    Invoke-Browser -BrowserArguments @("select", "#settingsLocaleSelect", "pt-BR")
    Invoke-Browser -BrowserArguments @("wait", "--text", "Conex$([char]0x00E3)o")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.documentElement.lang === 'pt-BR'"
    )
    # Every key the window asked for and no catalog answered. The console
    # warning translateApiMessage logs cannot stand in for this: the axe scan
    # collects errors, not warnings, so nothing else here would see a label
    # rendering as its own key.
    $missingKeys = Invoke-Browser -BrowserArguments @(
        "eval",
        "(window.__I18N_MISSING__ || []).join(', ')"
    ) | ConvertFrom-Json
    if ($missingKeys) {
        throw "The window asked for interface keys no catalog has: $missingKeys"
    }
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

    # Muting both sources is the one state the transport refuses: a session
    # opened there could only record silence, and it would still spend the
    # meeting's clock. The caption is what explains the dead button, which is
    # why it is asserted rather than left to the screenshot.
    Invoke-Browser -BrowserArguments @("find", "testid", "mute-microphone", "click")
    Invoke-Browser -BrowserArguments @("find", "testid", "mute-system", "click")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#captureToggleButton')?.disabled === true"
    )
    $mutedCaption = (Invoke-Browser -BrowserArguments @(
        "eval",
        "document.querySelector('#captureScopeCaption').textContent"
    ) | ConvertFrom-Json)
    if ($mutedCaption -ne "Ative uma fonte para transcrever") {
        throw "Muting both sources left the start caption reading '$mutedCaption'."
    }
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Assert-NoAccessibilityViolations -Screen "the capture screen with both sources muted"
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-muted.png"))
    Invoke-Browser -BrowserArguments @("find", "testid", "mute-microphone", "click")
    Invoke-Browser -BrowserArguments @("find", "testid", "mute-system", "click")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#captureToggleButton')?.disabled === false"
    )
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    $overlongTitle = "x" * 121
    Invoke-Browser -BrowserArguments @("eval", "document.querySelector('#sessionTitle').value = '$overlongTitle'")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Iniciar transcri$([char]0x00E7)$([char]0x00E3)o")
    Invoke-Browser -BrowserArguments @("wait", "--text", "O t$([char]0x00ED)tulo da reuni$([char]0x00E3)o n$([char]0x00E3)o $([char]0x00E9) v$([char]0x00E1)lido.")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    $startEnabled = (Invoke-Browser -BrowserArguments @("is", "enabled", "#captureToggleButton") | Select-Object -Last 1).Trim()
    if ($startEnabled -ne "true") {
        throw "The start button remained disabled after the rejected start request."
    }
    Invoke-Browser -BrowserArguments @("eval", "document.querySelector('#sessionTitle').value = ''")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")

    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Iniciar transcri$([char]0x00E7)$([char]0x00E3)o")
    Invoke-Browser -BrowserArguments @(
        "wait",
        "--fn",
        "document.querySelector('#captureToggleLabel')?.textContent === 'Parar transcri$([char]0x00E7)$([char]0x00E3)o'"
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

    # The signal meter replaced the histogram canvas: streaming has to light
    # bars through the live monitor -> SSE -> meter path. The fake backend
    # pumps a quiet microphone (0.04) next to a loud system source (0.5) --
    # see tests/visual_server.py -- so the bands below also pin each channel
    # to its own source: a swapped mapping reads loud where quiet belongs.
    # Nothing lighting up means the meter is not wired to the level stream,
    # the capture-state gate is stuck, or the lit style left the stylesheet.
    $streamingLitBars = "(() => { const lit = (id) => Array.from(document.querySelectorAll('#' + id + ' .capture-signal__bar')).filter((bar) => Number(getComputedStyle(bar).opacity) > 0.85).length; const mic = lit('microphoneChannel'); const sys = lit('systemChannel'); return mic >= 1 && mic <= 3 && sys >= 4; })()"
    Invoke-Browser -BrowserArguments @("wait", "--fn", $streamingLitBars)

    # Reduced motion must not mean a dead meter -- it means no transitions.
    Invoke-Browser -BrowserArguments @("set", "media", "dark", "reduced-motion")
    Invoke-Browser -BrowserArguments @("snapshot", "-i")
    Invoke-Browser -BrowserArguments @("wait", "--fn", $streamingLitBars)
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

    # The mid band -- a window narrower than the desktop run but wider than a
    # phone -- is where the one-line cards first run out of room, and where a
    # squeezed language column was once drawn over the channel name. The cards
    # must reflow (auto-fit) rather than overdraw.
    Invoke-Browser -BrowserArguments @("set", "viewport", "820", "900")
    Assert-TranscriptClearsTheDock
    $midViolation = Get-ChannelLayoutViolation
    if ($midViolation -gt 1) {
        throw "Channel card contents overlap or spill by ${midViolation}px at 820px wide."
    }
    # Side by side, not stacked: with the caption sr-only the transport is
    # just the toggle, and this width has room for both channels on one row.
    $midStack = [int](Invoke-Browser -BrowserArguments @(
        "eval",
        "(() => { const a = document.getElementById('microphoneChannel').getBoundingClientRect(); const b = document.getElementById('systemChannel').getBoundingClientRect(); return Math.round(Math.abs(a.top - b.top)); })()"
    ) | ConvertFrom-Json)
    if ($midStack -gt 2) {
        throw "The channel cards stack at 820px wide (top offset ${midStack}px) instead of sitting side by side."
    }
    # One line each, even for the widest locale name ("Áudio do sistema"):
    # the grid's column floor and the no-shrink titles are sized so a
    # side-by-side card never wraps a channel name.
    $nameHeights = Invoke-Browser -BrowserArguments @(
        "eval",
        "['microphoneChannel', 'systemChannel'].map((id) => Math.round(document.querySelector('#' + id + ' .capture-channel__name').getBoundingClientRect().height))"
    ) | ConvertFrom-Json
    $nameHeights = @($nameHeights)
    if ([int]$nameHeights[0] -gt 20 -or [int]$nameHeights[1] -gt 20) {
        throw "A channel name wraps at 820px wide (name heights: $($nameHeights -join 'px, ')px)."
    }
    Invoke-Browser -BrowserArguments @("screenshot", "--full", (Join-Path $artifactDirectory "capture-mid.png"))
    Invoke-Browser -BrowserArguments @("set", "viewport", "1440", "900")

    Invoke-Browser -BrowserArguments @("find", "role", "button", "click", "--name", "Parar transcri$([char]0x00E7)$([char]0x00E3)o")
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
