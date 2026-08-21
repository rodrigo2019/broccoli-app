(() => {
  "use strict";

  // The launch key arrives once, in the URL the native window opened. Read it,
  // then strip it from the address bar so it is not sitting in a visible URL for
  // the rest of the session. It never goes to storage and never leaves loopback.
  const CAPABILITY_KEY = new URLSearchParams(window.location.search).get("k") || "";
  if (CAPABILITY_KEY) {
    const clean = window.location.pathname + window.location.hash;
    window.history.replaceState(null, "", clean);
  }

  // An earlier version of the settings screen persisted the whole proxy panel
  // -- password included, in clear text -- under this key. The proxy now
  // lives behind /api/settings, with the password only ever in the Windows
  // Credential Manager, so this scrubs any copy an earlier version already
  // wrote to a returning user's machine. Idempotent: a no-op once removed.
  try {
    window.localStorage.removeItem("broccoli-desktop-settings");
  } catch {
    // Same best-effort contract as the rest of this file's localStorage use.
  }

  function apiHeaders(extra = {}) {
    return CAPABILITY_KEY ? { ...extra, "X-Broccoli-Key": CAPABILITY_KEY } : { ...extra };
  }

  function apiUrl(path) {
    return path;
  }

  // WebSocket cannot set a header either, so the key travels the same way here:
  // as a query parameter, over loopback only.
  function socketUrl(path) {
    const base = `ws://${window.location.host}${path}`;
    return CAPABILITY_KEY ? `${base}?k=${encodeURIComponent(CAPABILITY_KEY)}` : base;
  }

  const elements = {
    skipLink: document.querySelector("#skipLink"),
    loginView: document.querySelector("#loginView"),
    mainView: document.querySelector("#mainView"),
    settingsView: document.querySelector("#settingsView"),
    loginForm: document.querySelector("#loginForm"),
    tokenInput: document.querySelector("#tokenInput"),
    loginError: document.querySelector("#loginError"),
    sidebarFooter: document.querySelector("#sidebarFooter"),
    settingsButton: document.querySelector("#settingsButton"),
    logoutButton: document.querySelector("#logoutButton"),
    notification: document.querySelector("#notification"),
    newSessionButton: document.querySelector("#newSessionButton"),
    sessionsSentinel: document.querySelector("#sessionsSentinel"),
    sessionHistoryScroll: document.querySelector(".conversations-section-body"),
    sessionSearchInput: document.querySelector("#sessionSearchInput"),
    sessionHistorySection: document.querySelector("#sessionHistorySection"),
    sessionLibrary: document.querySelector("#sessionLibrary"),
    renameSessionModal: document.querySelector("#renameSessionModal"),
    renameSessionInput: document.querySelector("#renameSessionInput"),
    renameSessionCancel: document.querySelector("#renameSessionCancel"),
    renameSessionConfirm: document.querySelector("#renameSessionConfirm"),
    deleteSessionModal: document.querySelector("#deleteSessionModal"),
    deleteSessionCancel: document.querySelector("#deleteSessionCancel"),
    deleteSessionConfirm: document.querySelector("#deleteSessionConfirm"),
    backToTranscriptButton: document.querySelector("#backToTranscriptButton"),
    transcriptHeaderContext: document.querySelector("#transcriptHeaderContext"),
    settingsHeaderTitle: document.querySelector("#settingsHeaderTitle"),
    renameSessionButton: document.querySelector("#renameSessionButton"),
    captureIndicator: document.querySelector("#captureIndicator"),
    captureIndicatorLabel: document.querySelector("#captureIndicatorLabel"),
    settingsForm: document.querySelector("#settingsForm"),
    settingsRefreshDevicesButton: document.querySelector("#settingsRefreshDevicesButton"),
    settingsMicrophoneSelect: document.querySelector("#settingsMicrophoneSelect"),
    settingsSystemDeviceSelect: document.querySelector("#settingsSystemDeviceSelect"),
    settingsMicrophoneMeter: document.querySelector("#settingsMicrophoneMeter"),
    settingsSystemMeter: document.querySelector("#settingsSystemMeter"),
    settingsMicrophoneLevel: document.querySelector("#settingsMicrophoneLevel"),
    settingsSystemLevel: document.querySelector("#settingsSystemLevel"),
    settingsAudioTestStatus: document.querySelector("#settingsAudioTestStatus"),
    settingsAudioTestButton: document.querySelector("#settingsAudioTestButton"),
    themeLightOption: document.querySelector("#themeLightOption"),
    themeDarkOption: document.querySelector("#themeDarkOption"),
    proxyEnabled: document.querySelector("#proxyEnabled"),
    proxyFields: document.querySelector("#proxyFields"),
    proxyHost: document.querySelector("#proxyHost"),
    proxyPort: document.querySelector("#proxyPort"),
    proxyUsername: document.querySelector("#proxyUsername"),
    proxyPassword: document.querySelector("#proxyPassword"),
    proxyTestButton: document.querySelector("#proxyTestButton"),
    proxyTestStatus: document.querySelector("#proxyTestStatus"),
    resetSettingsButton: document.querySelector("#resetSettingsButton"),
    sessionTitle: document.querySelector("#sessionTitle"),
    sessionMeta: document.querySelector("#sessionMeta"),
    refreshDevicesButton: document.querySelector("#refreshDevicesButton"),
    deviceRequired: document.querySelector("#deviceRequired"),
    captureToggleButton: document.querySelector("#captureToggleButton"),
    copyCodeButton: document.querySelector("#copyCodeButton"),
    captureDock: document.querySelector("#captureDock"),
    jumpToLatestButton: document.querySelector("#jumpToLatestButton"),
    microphoneHistogram: document.querySelector("#microphoneHistogram"),
    systemHistogram: document.querySelector("#systemHistogram"),
    transcriptTimeline: document.querySelector("#transcriptTimeline"),
    transcriptProvisional: document.querySelector("#transcriptProvisional"),
    drawerToggle: document.querySelector("#drawer-toggle"),
  };

  // Same key and same values the platform writes, so the two surfaces agree on
  // what "dark" means and a theme picked in one reads naturally in the other.
  const THEME_STORAGE_KEY = "theme";

  // The proxy password is never part of this shape and never travels through
  // localStorage: it lives only in the Windows Credential Manager, reached
  // exclusively through /api/settings. This is what the server returns for an
  // unconfigured proxy, and what the form falls back to if that fetch fails.
  const DEFAULT_PROXY = { enabled: false, host: "", port: "", username: "" };

  async function loadProxySettings() {
    try {
      const data = await localFetch("/api/settings");
      state.proxy = data.proxy;
    } catch (error) {
      state.proxy = { ...DEFAULT_PROXY };
      reportError(error);
    }
  }

  function loadTheme() {
    try {
      return window.localStorage.getItem(THEME_STORAGE_KEY) === "light" ? "light" : "dark";
    } catch {
      return "dark";
    }
  }

  function applyTheme(theme) {
    const resolved = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", resolved);
    if (elements.themeLightOption) elements.themeLightOption.checked = resolved === "light";
    if (elements.themeDarkOption) elements.themeDarkOption.checked = resolved === "dark";
    // The histograms paint with colours resolved from CSS, so a theme change has
    // to invalidate what they cached.
    captureMotion?.refreshTheme();
  }

  function setTheme(theme) {
    state.theme = theme === "light" ? "light" : "dark";
    applyTheme(state.theme);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, state.theme);
    } catch {
      // Same best-effort contract as the rest of the local preferences.
    }
  }

  const state = {
    authenticated: false,
    sessions: [],
    nextCursor: null,
    searchQuery: "",
    searchTimer: null,
    selectedSession: null,
    selectedDevices: null,
    // The device pair chosen in the selects but not saved yet. Deliberately not
    // persisted: the server owns the durable selection, and a second stored copy
    // is exactly how the two drift apart.
    pendingDevices: { microphone_id: "", system_device_id: "" },
    connectionState: "idle",
    // Whether the transcript should ride along with new content. Decided by the
    // container's scroll position, not by the moment a row is appended, so a
    // user reading older lines is never yanked back down.
    followTranscript: true,
    transcriptHasNewContent: false,
    pendingDeltas: new Map(),
    eventSocket: null,
    eventRetryDelay: 0,
    sessionsLoading: false,
    sessionsRequestId: 0,
    segments: { cursor: null, loading: false, requestId: 0 },
    activeView: "transcript",
    // Populated from /api/settings when the settings screen opens -- never
    // from localStorage, and never carrying a password field.
    proxy: { ...DEFAULT_PROXY },
    theme: loadTheme(),
    audioTestActive: false,
    audioMeterBars: { microphone: [], system: [] },
    audioLevelSource: null,
    audioLevelRetry: null,
    renameSession: null,
    deleteSession: null,
  };

  // ---------------------------------------------------------------- notifications

  // Complete class names, never `alert-${type}`: the stylesheet is tree-shaken
  // against the markup at build time, so a class assembled at runtime is not in
  // it. Same map, same durations and same limit as the platform's base.js.
  const NOTIFICATION_STYLES = {
    success: { alertClass: "alert-success", icon: "bi bi-check-circle-fill" },
    error: { alertClass: "alert-error", icon: "bi bi-x-circle-fill" },
    warning: { alertClass: "alert-warning", icon: "bi bi-exclamation-triangle-fill" },
    info: { alertClass: "alert-info", icon: "bi bi-info-circle-fill" },
  };
  const NOTIFICATION_DURATIONS = { error: 7000, warning: 6000 };
  const NOTIFICATION_DEFAULT_DURATION = 5000;
  const NOTIFICATION_LIMIT = 4;
  const NOTIFICATION_FADE_MS = 400;

  function dismissNotification(alert) {
    if (!alert || alert.dataset.closing) return;
    alert.dataset.closing = "true";
    const timerId = Number(alert.dataset.timerId);
    if (timerId) window.clearTimeout(timerId);
    alert.classList.add("opacity-0", "-translate-y-4");
    window.setTimeout(() => alert.remove(), NOTIFICATION_FADE_MS);
  }

  function showNotification(message, type = "info", duration) {
    const container = elements.notification;
    if (!container) return null;

    const { alertClass, icon } = NOTIFICATION_STYLES[type] || NOTIFICATION_STYLES.info;
    const hideAfter =
      duration === undefined
        ? (NOTIFICATION_DURATIONS[type] ?? NOTIFICATION_DEFAULT_DURATION)
        : duration;

    const alert = document.createElement("div");
    alert.className = `alert ${alertClass} pointer-events-auto shadow-lg transition-all duration-300 opacity-0 -translate-y-4`;
    alert.setAttribute("role", type === "error" ? "alert" : "status");

    const body = document.createElement("div");
    body.className = "flex items-center gap-2";
    const iconElement = document.createElement("i");
    iconElement.className = `${icon} text-xl`;
    iconElement.setAttribute("aria-hidden", "true");
    const messageElement = document.createElement("span");
    // textContent, never innerHTML: these carry server details and device names.
    messageElement.textContent = message;
    body.append(iconElement, messageElement);

    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "btn btn-ghost btn-sm btn-circle";
    closeButton.setAttribute("aria-label", "Fechar");
    closeButton.innerHTML = '<i class="bi bi-x-lg" aria-hidden="true"></i>';
    closeButton.addEventListener("click", () => dismissNotification(alert));

    alert.append(body, closeButton);
    container.append(alert);

    // Drop the oldest so a burst of failures cannot fill the viewport. Count
    // only live nodes -- the ones already fading are on their way out.
    const live = Array.from(container.children).filter((node) => !node.dataset.closing);
    live.slice(0, -NOTIFICATION_LIMIT).forEach(dismissNotification);

    window.requestAnimationFrame(() => {
      alert.classList.remove("opacity-0", "-translate-y-4");
    });

    if (hideAfter > 0) {
      alert.dataset.timerId = String(window.setTimeout(() => dismissNotification(alert), hideAfter));
    }
    return alert;
  }

  function closeNotifications() {
    elements.notification?.querySelectorAll(".alert").forEach(dismissNotification);
  }

  // ------------------------------------------------------------------- capture motion

  const HISTOGRAM_CHANNELS = ["microphone", "system"];
  const HISTOGRAM_BARS = 34;
  // How much history one bar covers. Thirty-four bars at this rate is about a
  // second and a half of sound on screen.
  const HISTOGRAM_PUSH_MS = 45;
  // Share of the remaining distance a bar closes each frame. Low enough to round
  // off the twenty-hertz steps the level stream delivers, high enough that a
  // sudden loud sound still reads as sudden.
  const HISTOGRAM_EASE = 0.3;
  const HISTOGRAM_ACTIVE_STATES = ["starting", "streaming", "reconnecting"];

  function drawBar(context, x, y, width, height, radius) {
    if (typeof context.roundRect === "function") {
      context.beginPath();
      context.roundRect(x, y, width, height, radius);
      context.fill();
      return;
    }
    context.fillRect(x, y, width, height);
  }

  /**
   * Paints the two channel histograms in the capture panel.
   *
   * On a canvas rather than sixty DOM nodes: the level stream ticks about twenty
   * times a second, and answering each tick by writing sixty CSS custom
   * properties and restarting sixty transitions is a lot of style recalculation
   * for a decoration. Here a frame is a couple of dozen fills.
   *
   * What makes the motion read as sound rather than as a bar chart being redrawn
   * is that a bar eases toward the newest sample instead of stepping to it, and
   * that the history scrolls on a clock of its own instead of on packet arrival.
   *
   * The loop only runs while there is something to show. Idle, stopped, hidden
   * window, or a user who asked for less motion: no frames at all.
   */
  class CaptureMotion {
    constructor({ microphoneHistogram, systemHistogram }) {
      this.canvases = { microphone: microphoneHistogram, system: systemHistogram };
      this.contexts = { microphone: null, system: null };
      this.sizes = { microphone: { width: 0, height: 0 }, system: { width: 0, height: 0 } };
      this.samples = { microphone: [], system: [] };
      this.levels = { microphone: 0, system: 0 };
      this.targets = { microphone: 0, system: 0 };
      this.colors = { microphone: "", system: "" };
      this.captureState = "idle";
      this.signalActive = false;
      this.phase = 0;
      this.frame = null;
      this.lastFrameAt = 0;
      this.sincePush = 0;
      this.resizeObserver = null;
      this.reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)") || null;
    }

    mount() {
      for (const channel of HISTOGRAM_CHANNELS) {
        const canvas = this.canvases[channel];
        if (!canvas) continue;
        this.contexts[channel] = canvas.getContext("2d");
        this.samples[channel] = Array(HISTOGRAM_BARS).fill(0);
      }
      // Measured by an observer instead of read every frame: asking a canvas for
      // its client size mid-frame forces a layout that the rest of the frame
      // then waits on.
      if ("ResizeObserver" in window) {
        this.resizeObserver = new ResizeObserver(() => this.measure());
        for (const canvas of Object.values(this.canvases)) {
          if (canvas) this.resizeObserver.observe(canvas);
        }
      }
      this.measure();
      this.refreshTheme();
      this.reducedMotion?.addEventListener?.("change", () => this.sync());
      document.addEventListener("visibilitychange", () => this.sync());
      this.paint();
    }

    measure() {
      const ratio = window.devicePixelRatio || 1;
      for (const channel of HISTOGRAM_CHANNELS) {
        const canvas = this.canvases[channel];
        if (!canvas) continue;
        const width = Math.max(1, Math.floor(canvas.clientWidth));
        const height = Math.max(1, Math.floor(canvas.clientHeight));
        this.sizes[channel] = { width, height };
        const deviceWidth = Math.floor(width * ratio);
        const deviceHeight = Math.floor(height * ratio);
        if (canvas.width !== deviceWidth || canvas.height !== deviceHeight) {
          canvas.width = deviceWidth;
          canvas.height = deviceHeight;
        }
        this.contexts[channel]?.setTransform(ratio, 0, 0, ratio, 0, 0);
      }
      this.paint();
    }

    /** Re-read the colour each channel inherits from the stylesheet. */
    refreshTheme() {
      for (const channel of HISTOGRAM_CHANNELS) {
        const canvas = this.canvases[channel];
        if (canvas) this.colors[channel] = window.getComputedStyle(canvas).color;
      }
      this.paint();
    }

    setState(connectionState) {
      this.captureState = connectionState;
      this.sync();
    }

    isActive() {
      return HISTOGRAM_ACTIVE_STATES.includes(this.captureState);
    }

    setLevels(snapshot = {}) {
      this.signalActive = Boolean(snapshot.active);
      for (const channel of HISTOGRAM_CHANNELS) {
        const measurement = channel === "microphone" ? snapshot.microphone : snapshot.system;
        this.targets[channel] = Math.max(0, Math.min(1, Number(measurement?.level) || 0));
      }
      this.sync();
      // With the animation off, the history still has to advance -- just once per
      // delivered snapshot rather than once per frame.
      if (this.frame === null) this.step(HISTOGRAM_PUSH_MS, { instant: true });
    }

    /** Start or stop the frame loop to match what there is to show. */
    sync() {
      const running = this.signalActive && this.isActive();
      const animate = running && !document.hidden && !this.reducedMotion?.matches;
      if (animate && this.frame === null) {
        this.lastFrameAt = performance.now();
        this.frame = window.requestAnimationFrame((now) => this.tick(now));
        return;
      }
      if (!animate && this.frame !== null) {
        window.cancelAnimationFrame(this.frame);
        this.frame = null;
        if (!running) this.settle();
      }
    }

    tick(now) {
      this.frame = null;
      // Capped so a window that was hidden for a minute does not replay a minute
      // of history in a single frame.
      const elapsed = Math.min(now - this.lastFrameAt, 100);
      this.lastFrameAt = now;
      this.step(elapsed);
      this.sync();
    }

    step(elapsed, { instant = false } = {}) {
      const active = this.signalActive && this.isActive();
      for (const channel of HISTOGRAM_CHANNELS) {
        const target = active ? this.targets[channel] : 0;
        this.levels[channel] = instant
          ? target
          : this.levels[channel] + (target - this.levels[channel]) * HISTOGRAM_EASE;
      }
      this.phase += elapsed * 0.004;
      this.sincePush += elapsed;
      while (this.sincePush >= HISTOGRAM_PUSH_MS) {
        this.sincePush -= HISTOGRAM_PUSH_MS;
        for (const channel of HISTOGRAM_CHANNELS) {
          const samples = this.samples[channel];
          samples.push(this.levels[channel]);
          if (samples.length > HISTOGRAM_BARS) samples.shift();
        }
      }
      this.paint();
    }

    /** Drain the history so a stopped capture flattens instead of freezing. */
    settle() {
      for (const channel of HISTOGRAM_CHANNELS) {
        this.levels[channel] = 0;
        this.targets[channel] = 0;
        this.samples[channel] = Array(HISTOGRAM_BARS).fill(0);
      }
      this.paint();
    }

    paint() {
      for (const channel of HISTOGRAM_CHANNELS) {
        const context = this.contexts[channel];
        const { width, height } = this.sizes[channel];
        if (!context || !width || !height) continue;
        context.clearRect(0, 0, width, height);
        context.fillStyle = this.colors[channel] || "currentColor";

        const samples = this.samples[channel];
        const slot = width / samples.length;
        const barWidth = Math.max(1, slot * 0.62);
        const radius = barWidth / 2;
        const floor = Math.min(2, height);
        samples.forEach((value, index) => {
          const envelope = Math.min(1, Math.sqrt(Math.max(0, value)) * 1.6);
          // A travelling wobble rather than a fixed comb: at a steady level the
          // shape still moves, which is what keeps it from looking frozen.
          const profile = 0.82 + 0.18 * Math.sin(index * 0.9 + this.phase);
          const barHeight = Math.max(floor, envelope * height * profile);
          context.globalAlpha = 0.4 + 0.6 * envelope;
          drawBar(context, index * slot, height - barHeight, barWidth, barHeight, radius);
        });
        context.globalAlpha = 1;
      }
    }
  }

  const captureMotion = new CaptureMotion(elements);
  captureMotion.mount();
  applyTheme(state.theme);

  // ---------------------------------------------------------------- audio levels

  const AUDIO_METER_SEGMENTS = 18;
  const AUDIO_LEVEL_RETRY_MS = 2000;

  function mountAudioMeter(container, channel) {
    if (!container) return [];
    const bars = [];
    container.replaceChildren();
    for (let index = 0; index < AUDIO_METER_SEGMENTS; index += 1) {
      const bar = document.createElement("span");
      bar.className = "audio-level-card__segment";
      bar.dataset.index = String(index);
      bar.style.setProperty(
        "--segment-height",
        `${Math.round(34 + (index / Math.max(AUDIO_METER_SEGMENTS - 1, 1)) * 66)}%`,
      );
      bar.setAttribute("aria-hidden", "true");
      container.append(bar);
      bars.push(bar);
    }
    state.audioMeterBars[channel] = bars;
    return bars;
  }

  function meterAmount(value) {
    return Math.min(1, Math.sqrt(Math.max(0, Number(value) || 0)) * 1.6);
  }

  function formatDecibels(value) {
    if (!value || value < 0.003) return "−∞ dB";
    return `${Math.max(-48, Math.round(20 * Math.log10(value)))} dB`;
  }

  function renderAudioMeter(channel, measurement = {}, active = false) {
    const isMicrophone = channel === "microphone";
    const container = isMicrophone ? elements.settingsMicrophoneMeter : elements.settingsSystemMeter;
    const reading = isMicrophone ? elements.settingsMicrophoneLevel : elements.settingsSystemLevel;
    const bars = state.audioMeterBars[channel];
    const level = active ? Math.max(0, Math.min(1, Number(measurement.level) || 0)) : 0;
    const peak = active ? Math.max(level, Math.min(1, Number(measurement.peak) || 0)) : 0;
    const activeBars = Math.ceil(meterAmount(level) * bars.length);
    const peakBar = Math.max(0, Math.ceil(meterAmount(peak) * bars.length) - 1);
    bars.forEach((bar, index) => {
      bar.classList.toggle("is-active", index < activeBars);
      bar.classList.toggle("is-peak", active && index === peakBar && peak > 0);
    });
    const percent = Math.round(level * 100);
    container.setAttribute("aria-valuenow", String(percent));
    container.setAttribute(
      "aria-valuetext",
      active ? `${formatDecibels(level)}, pico ${formatDecibels(peak)}` : "Teste não iniciado",
    );
    reading.textContent = active ? formatDecibels(level) : "Aguardando";
  }

  function renderAudioTestControls() {
    elements.settingsAudioTestButton.textContent = state.audioTestActive
      ? "Parar teste"
      : "Testar entrada e saída";
    elements.settingsAudioTestButton.classList.toggle("btn-error", state.audioTestActive);
    elements.settingsAudioTestButton.classList.toggle("btn-outline", !state.audioTestActive);
  }

  function finishAudioTest(message = "Escolha os dispositivos e inicie o teste.") {
    state.audioTestActive = false;
    renderAudioMeter("microphone");
    renderAudioMeter("system");
    elements.settingsAudioTestStatus.textContent = message;
    renderAudioTestControls();
  }

  function handleAudioLevelSnapshot(levels) {
    if (!levels || typeof levels !== "object") return;
    captureMotion.setLevels(levels);
    if (!state.audioTestActive) return;
    if (!levels.active) {
      finishAudioTest(
        "O teste foi interrompido. Selecione os dispositivos novamente para tentar de novo.",
      );
      return;
    }
    renderAudioMeter("microphone", levels.microphone, true);
    renderAudioMeter("system", levels.system, true);
  }

  function closeAudioLevelStream() {
    window.clearTimeout(state.audioLevelRetry);
    state.audioLevelRetry = null;
    const source = state.audioLevelSource;
    state.audioLevelSource = null;
    source?.close();
  }

  /**
   * Open the one audio-level route.
   *
   * Without device IDs the stream is a passive observer feeding the capture
   * footer. With them it *is* the settings device check: the server opens the
   * meter for this connection and releases it when the connection ends, so
   * stopping the test is just closing the stream -- no second request that a
   * closing window might never send.
   */
  function connectAudioLevels(deviceTest = null) {
    if (!state.authenticated || !("EventSource" in window)) return;
    closeAudioLevelStream();
    // EventSource cannot set a header, so the key rides along as a query
    // parameter here too -- alongside any device-test parameters already in
    // play, not in place of them. (Unlike socketUrl, this keeps the http(s)
    // scheme EventSource requires; a ws:// URL is not a valid one for it.)
    const params = new URLSearchParams(deviceTest || {});
    if (CAPABILITY_KEY) params.set("k", CAPABILITY_KEY);
    const query = params.toString() ? `?${params.toString()}` : "";
    const source = new EventSource(`/api/audio-levels/stream${query}`);
    state.audioLevelSource = source;

    source.addEventListener("message", (event) => {
      if (state.audioLevelSource !== source) return;
      try {
        handleAudioLevelSnapshot(JSON.parse(event.data));
      } catch {
        showNotification("Não foi possível processar o nível de áudio.", "error");
      }
    });

    source.addEventListener("device_error", (event) => {
      if (state.audioLevelSource !== source) return;
      let detail = null;
      try {
        detail = JSON.parse(event.data).detail || null;
      } catch {
        // Keep the generic message; the stream is ending either way.
      }
      // `detail` is the API's English text, so it has to go through the same
      // lookup as every other API error before it reaches the screen.
      finishAudioTest(detail ? translateApiMessage(detail) : "Não foi possível iniciar o teste de áudio.");
      // The server closes the stream after this, and a completed stream is one
      // EventSource happily reopens -- which would retry the dead device on a
      // loop. Reopen the passive observer instead.
      connectAudioLevels();
    });

    source.addEventListener("error", () => {
      if (state.audioLevelSource !== source) return;
      // readyState CONNECTING means the browser is retrying on its own.
      if (source.readyState !== EventSource.CLOSED) return;
      state.audioLevelSource = null;
      if (deviceTest) {
        finishAudioTest("Não foi possível iniciar o teste de áudio.");
        connectAudioLevels();
        return;
      }
      state.audioLevelRetry = window.setTimeout(() => {
        if (state.authenticated && state.audioLevelSource === null) connectAudioLevels();
      }, AUDIO_LEVEL_RETRY_MS);
    });
  }

  function startAudioTest() {
    const microphoneId = elements.settingsMicrophoneSelect.value;
    const systemDeviceId = elements.settingsSystemDeviceSelect.value;
    if (!microphoneId || !systemDeviceId) {
      finishAudioTest("Escolha um microfone e uma saída de áudio antes de testar.");
      return;
    }
    // The server refuses this with a 409, but a refused EventSource surfaces as
    // a bare connection error with no body to read -- so the one refusal the
    // user can act on is caught here, where the reason is still known.
    if (isCaptureActive()) {
      finishAudioTest("Pare a captura antes de testar os dispositivos.");
      return;
    }
    state.audioTestActive = true;
    elements.settingsAudioTestStatus.textContent =
      "Teste em execução. Fale no microfone e reproduza um som no computador.";
    renderAudioTestControls();
    connectAudioLevels({ microphone_id: microphoneId, system_device_id: systemDeviceId });
  }

  function stopAudioTest(message = "Teste de áudio encerrado.") {
    const wasActive = state.audioTestActive;
    finishAudioTest(message);
    if (!wasActive) return;
    connectAudioLevels();
  }

  mountAudioMeter(elements.settingsMicrophoneMeter, "microphone");
  mountAudioMeter(elements.settingsSystemMeter, "system");
  renderAudioMeter("microphone");
  renderAudioMeter("system");
  renderAudioTestControls();

  // ------------------------------------------------------------------------ fetch

  // The API keeps returning machine-readable English detail strings -- that is the
  // right contract for an API. The interface is pt-BR, so the mapping lives here.
  const API_MESSAGES = {
    "The session title is invalid.": "O título da reunião não é válido.",
    "A capture is active.": "Já existe uma captura em andamento.",
    "Local host required.": "Esta ação só pode partir do aplicativo.",
    "Local origin required.": "Esta ação só pode partir do aplicativo.",
    "Local key required.": "Esta ação só pode partir do aplicativo.",
    "Proxy host and port are required.": "Informe o endereço e a porta do proxy.",
    "Credential storage is unavailable.": "O armazenamento de credenciais não está disponível.",
  };

  function translateApiMessage(detail) {
    const message = API_MESSAGES[detail];
    if (!message) {
      // Every detail the table above does not cover lands here. The fallback
      // has to say something a user can act on, and the gap has to stay
      // discoverable instead of silently showing a generic toast forever.
      console.warn(`Unmapped API error detail: ${JSON.stringify(detail)}`);
    }
    return message || "Não foi possível concluir a ação. Tente novamente.";
  }

  async function localFetch(path, options = {}) {
    const response = await fetch(apiUrl(path), {
      headers: { "Content-Type": "application/json", ...apiHeaders(options.headers || {}) },
      ...options,
    });
    if (response.ok) {
      return response.status === 204 ? null : response.json();
    }
    if (response.status === 401) closeAudioLevelStream();
    const payload = await response.json().catch(() => ({}));
    // The raw English detail travels on the error itself so reportError can
    // route it through translateApiMessage -- a client-side validation error
    // (already pt-BR) is not tagged and passes through untouched instead.
    const error = new Error(payload.detail || "");
    error.isApiDetail = true;
    throw error;
  }

  function reportError(error) {
    const message = error?.isApiDetail ? translateApiMessage(error.message) : error.message;
    showNotification(message, "error");
  }

  function setLoginError(message = "") {
    elements.loginError.textContent = message;
    elements.loginError.classList.toggle("hidden", !message);
  }

  // ------------------------------------------------------------------------ views

  /** Move focus into a screen that just became visible.
   *
   * Every view change used to leave focus on <body>, so the next Tab restarted
   * from the top of a document with ~96 focusable elements -- about 82 of them
   * sidebar rows -- and a screen reader was told nothing had happened. The rename
   * dialog already does this correctly; this is the same behaviour everywhere
   * else. */
  function focusScreen(container) {
    if (!container) return;
    const heading = container.querySelector("h1, h2");
    const target =
      heading ||
      container.querySelector("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
    if (!target) return;
    if (target === heading) target.setAttribute("tabindex", "-1");
    target.focus();
  }

  function renderView() {
    const settingsOpen = state.authenticated && state.activeView === "settings";
    elements.loginView.classList.toggle("hidden", state.authenticated);
    elements.mainView.classList.toggle("hidden", !state.authenticated || settingsOpen);
    // #mainView is what the skip link jumps to, and it carries `hidden` on the
    // login screen -- focusing a heading inside a display:none ancestor is a
    // silent no-op. Keeping the link itself hidden pre-login means there is
    // nothing to skip to yet, and it also drops out of tab order, so the very
    // first Tab on the login screen lands on the token field, not a dead link.
    elements.skipLink.classList.toggle("hidden", !state.authenticated);
    elements.settingsView.classList.toggle("hidden", !settingsOpen);
    elements.sidebarFooter.classList.toggle("hidden", !state.authenticated);
    elements.newSessionButton.classList.toggle("hidden", !state.authenticated);
    elements.sessionHistorySection.classList.toggle("hidden", !state.authenticated);
    elements.transcriptHeaderContext.classList.toggle("hidden", settingsOpen || !state.authenticated);
    elements.settingsHeaderTitle.classList.toggle("hidden", !settingsOpen);
    elements.backToTranscriptButton.classList.toggle("hidden", !settingsOpen);
    // The capture badge and the code button describe an open session, so the
    // login screen and the settings screen show neither.
    const sessionActionsVisible = state.authenticated && !settingsOpen;
    elements.captureIndicator.classList.toggle("hidden", !sessionActionsVisible);
    elements.copyCodeButton.classList.toggle("hidden", !sessionActionsVisible);
    elements.settingsButton.setAttribute("aria-current", settingsOpen ? "page" : "false");
  }

  function closeDrawer() {
    if (elements.drawerToggle) elements.drawerToggle.checked = false;
  }

  function renderDevices(devices) {
    const selected = state.selectedDevices || {};
    const microphoneId = selected.microphone_id || state.pendingDevices.microphone_id;
    const systemDeviceId = selected.system_device_id || state.pendingDevices.system_device_id;
    renderDeviceSelect(
      elements.settingsMicrophoneSelect,
      devices.filter((device) => device.kind === "mic"),
      microphoneId,
    );
    renderDeviceSelect(
      elements.settingsSystemDeviceSelect,
      devices.filter((device) => device.kind === "system"),
      systemDeviceId,
    );
    updateDeviceRequirement();
  }

  function updateDeviceRequirement() {
    const required =
      !state.selectedDevices?.microphone_id || !state.selectedDevices?.system_device_id;
    elements.deviceRequired.classList.toggle("hidden", !required);
  }

  function renderDeviceSelect(select, devices, selectedDeviceId) {
    select.replaceChildren();
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Selecionar dispositivo";
    empty.title = empty.textContent;
    select.append(empty);
    for (const device of devices) {
      const option = document.createElement("option");
      option.value = device.device_id;
      option.textContent = device.label;
      option.title = device.label;
      option.selected = device.device_id === selectedDeviceId;
      select.append(option);
    }
    syncDeviceSelectTitle(select);
  }

  function syncDeviceSelectTitle(select) {
    select.title = select.selectedOptions[0]?.textContent || "";
  }

  function updateProxyFieldsVisibility() {
    elements.proxyFields.classList.toggle("hidden", !elements.proxyEnabled.checked);
  }

  function renderSettings() {
    elements.proxyEnabled.checked = state.proxy.enabled;
    elements.proxyHost.value = state.proxy.host;
    elements.proxyPort.value = state.proxy.port;
    elements.proxyUsername.value = state.proxy.username;
    // The saved password never comes back from the server -- this field starts
    // empty on every render and stays that way unless the user types a new one.
    elements.proxyPassword.value = "";
    elements.proxyTestStatus.textContent = "";
    updateProxyFieldsVisibility();
    renderAudioTestControls();
  }

  /**
   * Open the settings screen.
   *
   * `focusTarget`, when given a real element, is focused instead of the
   * screen's own heading -- used by startSession() below to land focus on
   * the microphone selector when the redirect here is the answer to "why
   * didn't capture start", rather than on a heading that says nothing about
   * that. The `instanceof` guard is what keeps this safe as a bare listener:
   * `settingsButton` and `refreshDevicesButton` both register this function
   * directly, so their click event lands in this parameter too, and a
   * PointerEvent is not an HTMLElement -- ordinary navigation still falls
   * through to focusScreen exactly as before.
   *
   * The proxy panel renders twice: once immediately with whatever was loaded
   * last (so the rest of the screen -- audio devices, theme -- never waits on
   * a network round trip), then again once the fresh /api/settings read lands.
   */
  async function showSettings(focusTarget) {
    if (!state.authenticated) return;
    state.activeView = "settings";
    closeDrawer();
    renderView();
    renderSettings();
    if (focusTarget instanceof HTMLElement) focusTarget.focus();
    else focusScreen(elements.settingsView);
    await loadProxySettings();
    renderSettings();
  }

  function showTranscript() {
    state.activeView = "transcript";
    renderView();
    stopAudioTest();
    focusScreen(elements.mainView);
  }

  /**
   * Build the /api/settings proxy payload from the form.
   *
   * The password is included only when the user actually typed one -- an
   * empty field means "keep whatever is already in the vault", never "clear
   * it", since the saved password never round-trips here for the user to see
   * and re-enter.
   */
  function collectProxyPayload() {
    if (!elements.proxyEnabled.checked) return { enabled: false };
    const payload = {
      enabled: true,
      host: elements.proxyHost.value.trim(),
      port: Number(elements.proxyPort.value.trim()) || 0,
      username: elements.proxyUsername.value.trim(),
    };
    const password = elements.proxyPassword.value;
    if (password) payload.password = password;
    return payload;
  }

  async function saveProxySettings() {
    await localFetch("/api/settings", {
      method: "POST",
      body: JSON.stringify({ proxy: collectProxyPayload() }),
    });
    await loadProxySettings();
  }

  async function saveDeviceSelection() {
    const microphoneId = elements.settingsMicrophoneSelect.value;
    const systemDeviceId = elements.settingsSystemDeviceSelect.value;
    if (!microphoneId && !systemDeviceId) {
      await localFetch("/api/devices/selection", { method: "DELETE" });
      state.selectedDevices = null;
      return;
    }
    if (!microphoneId || !systemDeviceId) {
      throw new Error("Selecione um microfone e uma saída de áudio para salvar a configuração.");
    }
    await localFetch("/api/devices/selection", {
      method: "PUT",
      body: JSON.stringify({ microphone_id: microphoneId, system_device_id: systemDeviceId }),
    });
    state.selectedDevices = { microphone_id: microphoneId, system_device_id: systemDeviceId };
  }

  async function saveSettings(event) {
    event.preventDefault();
    setTheme(elements.themeLightOption.checked ? "light" : "dark");
    try {
      await saveDeviceSelection();
      await saveProxySettings();
    } catch (error) {
      reportError(error);
      return;
    }
    updateDeviceRequirement();
    renderSettings();
    showNotification("Configurações salvas nesta máquina.", "success");
  }

  async function resetSettings() {
    setTheme("dark");
    elements.settingsMicrophoneSelect.value = "";
    elements.settingsSystemDeviceSelect.value = "";
    state.pendingDevices = { microphone_id: "", system_device_id: "" };
    stopAudioTest("Configurações restauradas. O teste de áudio foi encerrado.");
    try {
      await localFetch("/api/devices/selection", { method: "DELETE" });
      // Disabling here also deletes the stored proxy password server-side --
      // "restore defaults" must not leave a credential behind.
      await localFetch("/api/settings", {
        method: "POST",
        body: JSON.stringify({ proxy: { enabled: false } }),
      });
    } catch (error) {
      reportError(error);
      return;
    }
    state.selectedDevices = null;
    await loadProxySettings();
    renderSettings();
    updateDeviceRequirement();
    showNotification("Configurações restauradas.", "info");
  }

  async function testProxyConnection() {
    const host = elements.proxyHost.value.trim();
    const port = Number(elements.proxyPort.value.trim()) || 0;
    if (!host || !port) {
      elements.proxyTestStatus.textContent = "Informe o endereço e a porta antes de testar.";
      return;
    }
    elements.proxyTestButton.disabled = true;
    elements.proxyTestStatus.textContent = "Testando conexão...";
    try {
      const result = await localFetch("/api/settings/test-proxy", {
        method: "POST",
        body: JSON.stringify({
          host,
          port,
          username: elements.proxyUsername.value.trim(),
          // Same rule as saving: an empty field tests without a password
          // rather than silently reusing whatever is already stored.
          password: elements.proxyPassword.value || undefined,
        }),
      });
      elements.proxyTestStatus.textContent = result.ok
        ? "Conexão bem-sucedida."
        : "Não foi possível conectar através do proxy.";
    } catch {
      elements.proxyTestStatus.textContent = "Não foi possível conectar através do proxy.";
    } finally {
      elements.proxyTestButton.disabled = false;
    }
  }

  // ------------------------------------------------------------------- session list

  function sessionLabel(session) {
    return session.title || session.device_label || session.uuid_code;
  }

  /**
   * Parse a serialized timestamp into an instant, or 0 when it is missing.
   *
   * Comparing these as text is what used to put the wrong session on top. The
   * backend renders a timestamp with microseconds only when it has them, and
   * `localeCompare` treats the separating dot as ignorable punctuation, so
   * `…:00Z` and `…:00.9Z` came back in the wrong order. Instants have no such
   * opinions, and they also make `+00:00` and `Z` the same moment.
   */
  function sessionTimestamp(value) {
    const parsed = Date.parse(value || "");
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  /** "20 ago" -- the day a session started, for the row itself and its group heading. */
  function formatSessionDate(startedAt) {
    return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "short" }).format(
      new Date(startedAt),
    );
  }

  /** "42 min" for an ended session, or "" while it is still open (no `ended_at` yet). */
  function formatSessionDuration(startedAt, endedAt) {
    if (!endedAt) return "";
    const minutes = Math.max(1, Math.round((new Date(endedAt) - new Date(startedAt)) / 60000));
    return `${minutes} min`;
  }

  /** "1 segmento" vs "3 segmentos" -- the header tooltip had this hardcoded plural. */
  function segmentCountLabel(count) {
    return count === 1 ? "1 segmento" : `${count} segmentos`;
  }

  /**
   * Calendar-day key used to group session rows in the sidebar, in the
   * viewer's own timezone rather than UTC -- grouping by the UTC date would
   * put a 11pm-local meeting under tomorrow's heading for anyone west of
   * Greenwich. Empty when there is no parseable `started_at`, so a session
   * missing that field renders without a group heading instead of a bogus one.
   */
  function sessionDateGroupKey(session) {
    const timestamp = sessionTimestamp(session.started_at);
    if (!timestamp) return "";
    const date = new Date(timestamp);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }

  /** Pinned first, then most recently created. Mirrors the backend ordering. */
  function sortSessions() {
    state.sessions.sort((left, right) => {
      if (Boolean(left.is_pinned) !== Boolean(right.is_pinned)) {
        return left.is_pinned ? -1 : 1;
      }
      if (left.is_pinned) {
        const pinDelta = sessionTimestamp(right.pinned_at) - sessionTimestamp(left.pinned_at);
        if (pinDelta) return pinDelta;
      }
      const startDelta = sessionTimestamp(right.started_at) - sessionTimestamp(left.started_at);
      if (startDelta) return startDelta;
      return String(left.uuid_code).localeCompare(String(right.uuid_code));
    });
  }

  function icon(name, extraClasses = "") {
    const element = document.createElement("i");
    element.className = `bi bi-${name}${extraClasses ? ` ${extraClasses}` : ""}`;
    element.setAttribute("aria-hidden", "true");
    return element;
  }

  /**
   * Swap an existing icon's glyph in place, the same base ("bi") plus variant
   * ("bi-<name>") composition icon() uses to build one from scratch. A plain
   * `element.className = ...` reassignment would work today, but it silently
   * drops any other class a future caller adds to that element on the next
   * reconciliation pass -- the same failure mode the disabled-state fix
   * elsewhere in this file exists to avoid.
   */
  function setIconName(element, name) {
    for (const token of Array.from(element.classList)) {
      if (token === "bi" || token.startsWith("bi-")) element.classList.remove(token);
    }
    element.classList.add("bi", `bi-${name}`);
  }

  function pinIcon() {
    const element = icon("pin-angle-fill", "session-row-pin shrink-0 text-primary text-xs");
    element.removeAttribute("aria-hidden");
    element.setAttribute("aria-label", "Sessão fixada");
    element.title = "Sessão fixada";
    return element;
  }

  function closeSessionMenu(trigger) {
    trigger?.focus();
  }

  function addSessionMenuAction(menu, { label, iconName, className = "", disabled = false, onClick }) {
    const item = document.createElement("li");
    const action = document.createElement("button");
    action.type = "button";
    action.className = `session-menu-action ${className}`.trim();
    action.disabled = disabled;
    const iconElement = icon(iconName);
    const text = document.createElement("span");
    text.textContent = label;
    action.append(iconElement, text);
    action.addEventListener("click", (event) => {
      event.stopPropagation();
      // Read the live property, not the `disabled` this closed over: the row
      // that owns this button is reused across renders (see renderSessions),
      // and updateSessionActionMenu flips it directly on the element.
      if (!action.disabled) onClick();
    });
    item.append(action);
    menu.append(item);
    return { action, iconElement, text };
  }

  /**
   * Build the "..." menu for one row.
   *
   * `holder` carries the row's current session so these click handlers always
   * act on the latest data even after the row's DOM is reused for an updated
   * session (see buildSessionRow/updateSessionRow) -- closing over `session`
   * itself would go stale the moment a rename or pin toggle replaces that
   * object in state.sessions.
   */
  function sessionActionMenu(holder) {
    const dropdown = document.createElement("div");
    dropdown.className = "dropdown dropdown-end session-row-actions";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "session-menu-trigger btn btn-sm h-[30px] w-[30px] min-h-0 bg-transparent";
    trigger.title = "Opções da sessão";
    trigger.append(icon("three-dots"));
    const menu = document.createElement("ul");
    menu.className = "dropdown-content menu z-[1] w-36 rounded-box bg-base-100 p-1 shadow";
    menu.tabIndex = 0;

    const pinRefs = addSessionMenuAction(menu, {
      label: "Fixar",
      iconName: "pin-angle-fill",
      onClick: () => {
        closeSessionMenu(trigger);
        updateSessionMetadata(holder.session, { is_pinned: !holder.session.is_pinned }).catch(reportError);
      },
    });
    addSessionMenuAction(menu, {
      label: "Renomear",
      iconName: "pencil",
      onClick: () => {
        closeSessionMenu(trigger);
        openRenameSession(holder.session);
      },
    });
    const deleteRefs = addSessionMenuAction(menu, {
      label: "Excluir",
      iconName: "trash",
      className: "text-error",
      onClick: () => {
        closeSessionMenu(trigger);
        openDeleteSession(holder.session);
      },
    });

    dropdown.append(trigger, menu);
    const preventSessionSelection = (event) => event.stopPropagation();
    dropdown.addEventListener("pointerdown", preventSessionSelection);
    dropdown.addEventListener("mousedown", preventSessionSelection);
    dropdown.addEventListener("mouseup", preventSessionSelection);
    dropdown.addEventListener("click", preventSessionSelection);
    return { dropdown, trigger, pinRefs, deleteRefs };
  }

  /**
   * Refresh the parts of the "..." menu that can change after the row was
   * built: the trigger's label, the pin action's label/icon, and whether
   * delete is blocked by a live capture.
   */
  function updateSessionActionMenu(menuRefs, session) {
    const label = sessionLabel(session);
    menuRefs.trigger.setAttribute("aria-label", `Opções para ${label}`);
    menuRefs.pinRefs.text.textContent = session.is_pinned ? "Desafixar" : "Fixar";
    setIconName(menuRefs.pinRefs.iconElement, session.is_pinned ? "pin-angle" : "pin-angle-fill");
    menuRefs.deleteRefs.text.textContent = session.is_live ? "Excluir sessão ativa" : "Excluir";
    menuRefs.deleteRefs.action.disabled = session.is_live;
    if (session.is_live) {
      menuRefs.deleteRefs.action.title = "Pare a captura antes de excluir esta sessão.";
    } else {
      menuRefs.deleteRefs.action.removeAttribute("title");
    }
  }

  function sessionListPlaceholder({ iconName, title, description, spinner = false }) {
    const item = document.createElement("li");
    const block = document.createElement("div");
    // `block` is load-bearing: DaisyUI lays out a `.menu li`'s direct child as a
    // column-flow grid, which would put the icon beside the text instead of
    // above it. The utility layer wins over the daisyui layer.
    block.className = "block px-3 py-8 text-center";
    if (spinner) {
      const loading = document.createElement("span");
      loading.className = "loading loading-spinner loading-md text-primary";
      block.append(loading);
    } else {
      const badge = document.createElement("div");
      badge.className =
        "mx-auto flex h-14 w-14 items-center justify-center rounded-3xl bg-base-300 text-2xl text-base-content/30";
      badge.append(icon(iconName));
      block.append(badge);
    }
    const heading = document.createElement("p");
    heading.className = "mt-3 text-sm font-semibold";
    heading.textContent = title;
    block.append(heading);
    if (description) {
      const hint = document.createElement("p");
      hint.className = "mt-1 text-xs text-base-content/70";
      hint.textContent = description;
      block.append(hint);
    }
    item.append(block);
    return item;
  }

  /**
   * Build one sidebar row's DOM.
   *
   * Only structure -- updateSessionRow fills in everything that can change
   * across a render, and runs immediately after this for a freshly built row
   * too, so nothing here needs to duplicate that content.
   */
  function buildSessionRow(session) {
    // The click/keydown handlers below close over `holder`, not `session`:
    // this row's DOM is kept and reused by renderSessions on every later
    // render (that reuse is what keeps a focused row from being torn out from
    // under the keyboard), and updateSessionRow repoints holder.session to
    // the latest object so a stale rename or pin state is never acted on.
    const holder = { session };

    const item = document.createElement("li");
    item.className =
      "session-library-item flex min-w-0 items-center rounded-md text-md transition-all duration-200 hover:bg-base-300/50";
    item.dataset.sessionId = session.uuid_code;

    const content = document.createElement("div");
    content.className = "session-row-content flex w-full min-w-0 items-center";
    content.dataset.testid = `session-row-${session.uuid_code}`;
    content.setAttribute("role", "button");
    content.tabIndex = 0;
    content.addEventListener("click", () => {
      selectSession(holder.session).catch(reportError);
    });
    content.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selectSession(holder.session).catch(reportError);
    });

    const rowContent = document.createElement("span");
    rowContent.className = "session-title-wrap flex min-w-0 grow flex-col overflow-hidden";
    const titleLine = document.createElement("span");
    titleLine.className = "flex min-w-0 items-center gap-1";
    const title = document.createElement("span");
    title.className = "block min-w-0 whitespace-nowrap overflow-hidden text-ellipsis";
    titleLine.append(title);
    rowContent.append(titleLine);
    // Date and duration, so a list of auto-generated names is still
    // navigable -- see updateSessionRow, which is the only place this ever
    // gets text.
    const metaLine = document.createElement("span");
    // /70, not the transcript row's /50: this text sits over the sidebar's
    // lighter background in the light theme, where /50 measured 3.08:1 --
    // under the 4.5:1 AA floor axe checks for on the settings screen.
    metaLine.className = "block text-xs text-base-content/70";
    rowContent.append(metaLine);
    content.append(rowContent);

    const menu = sessionActionMenu(holder);
    content.append(menu.dropdown);
    item.append(content);

    item.sessionRefs = { holder, content, titleLine, title, metaLine, menu };
    return item;
  }

  /** Patch an already-built row so it matches the given session's current data. */
  function updateSessionRow(item, session) {
    const refs = item.sessionRefs;
    refs.holder.session = session;
    item.classList.toggle("is-active", state.selectedSession?.uuid_code === session.uuid_code);
    const label = sessionLabel(session);
    refs.content.setAttribute("aria-label", `Abrir sessão ${label}`);
    refs.title.title = label;
    refs.title.textContent = label;
    const existingPin = refs.titleLine.querySelector(".session-row-pin");
    if (session.is_pinned && !existingPin) {
      refs.titleLine.prepend(pinIcon());
    } else if (!session.is_pinned && existingPin) {
      existingPin.remove();
    }
    const dateLabel = session.started_at ? formatSessionDate(session.started_at) : "";
    const durationLabel = formatSessionDuration(session.started_at, session.ended_at);
    refs.metaLine.textContent = durationLabel ? `${dateLabel} · ${durationLabel}` : dateLabel;
    refs.metaLine.classList.toggle("hidden", !dateLabel);
    updateSessionActionMenu(refs.menu, session);
  }

  /**
   * Build one sidebar section heading -- either "Fixadas" or a "20 ago" day
   * heading.
   *
   * Only structure, same split as buildSessionRow/updateSessionRow: this has
   * no interactive state to preserve, but it is still built once and patched
   * in place on later renders rather than recreated every time, so it does
   * not disturb the sibling session rows' position bookkeeping below.
   *
   * `key` is opaque here -- `"pinned"` or a `YYYY-MM-DD` string -- it only
   * has to be stable and unique per heading for the reconciliation map in
   * renderSessions() below. visual-check.ps1 relies on genuine day headings
   * being distinguishable from the pinned one, so it filters this attribute
   * by the `YYYY-MM-DD` shape rather than assuming every heading is a date.
   */
  function buildGroupHeadingRow(key) {
    const item = document.createElement("li");
    item.className =
      "session-group-heading px-3 pb-1 pt-3 text-[11px] font-semibold uppercase tracking-wide text-base-content/70 first:pt-1";
    item.dataset.groupKey = key;
    return item;
  }

  function updateGroupHeadingRow(item, label) {
    item.textContent = label;
  }

  /**
   * Sync the sidebar list to state.sessions, in place.
   *
   * A row -- and the "..." trigger inside it -- can hold keyboard focus.
   * Rebuilding the `<ul>` from scratch on every render (the previous
   * behaviour) tore that node down and replaced it with a lookalike, so
   * activating a row left focus stranded on <body>: the reviewer captured the
   * focused node before the click and found `document.contains(node)` false
   * afterwards. Keying rows by uuid_code and reusing their DOM nodes here is
   * what keeps the focused node alive across a re-render.
   */
  function renderSessions() {
    // The sentinel is both the trigger for the next page and the only loading
    // affordance the list needs; with no cursor left there is nothing to watch.
    elements.sessionsSentinel.classList.toggle("hidden", !state.nextCursor);

    if (!state.sessions.length) {
      elements.sessionLibrary.replaceChildren(
        state.sessionsLoading
          ? sessionListPlaceholder({ title: "Carregando sessões…", spinner: true })
          : sessionListPlaceholder({
              iconName: "mic",
              title: state.searchQuery ? "Nenhum resultado" : "Nenhuma sessão ainda",
              description: state.searchQuery
                ? "Tente outro termo de busca."
                : "Inicie uma captura para criar a primeira.",
            }),
      );
      return;
    }

    sortSessions();

    // Pinned sessions get their own labelled section ahead of the
    // day-grouped list, instead of taking part in the day grouping below.
    // They used to walk the same recency-then-day pass as everything else,
    // which is exactly what broke: a pin sorts to the top regardless of its
    // date, so the very next (unpinned, newest) session usually starts a
    // *different* day, and the pinned session's own day would then open a
    // second, non-adjacent heading once the unpinned block reached it --
    // one calendar day rendered as two separate, out-of-order groups. A
    // pinned item is found by being pinned, not by its date, so it does not
    // need a day heading at all -- its own row still shows its date (see
    // updateSessionRow), just not as a group heading.
    const pinnedSessions = state.sessions.filter((session) => session.is_pinned);
    const unpinnedSessions = state.sessions.filter((session) => !session.is_pinned);

    const renderItems = [];
    if (pinnedSessions.length) {
      renderItems.push({ kind: "group", key: "group:pinned", label: "Fixadas" });
      for (const session of pinnedSessions) {
        renderItems.push({ kind: "session", key: session.uuid_code, session });
      }
    }

    // One heading per calendar day the unpinned order crosses into.
    // Computed fresh every render from state.sessions, same as
    // sortSessions() itself -- this is what turns a flat 45-row list of
    // auto-generated names into something a day can be found in.
    let lastGroupKey = null;
    for (const session of unpinnedSessions) {
      const groupKey = sessionDateGroupKey(session);
      if (groupKey && groupKey !== lastGroupKey) {
        renderItems.push({ kind: "group", key: `group:${groupKey}`, label: formatSessionDate(session.started_at) });
        lastGroupKey = groupKey;
      }
      renderItems.push({ kind: "session", key: session.uuid_code, session });
    }

    // Index the rows already on screen by their key. Anything here without a
    // key is a leftover placeholder from the empty/loading state above, not a
    // row -- it gets dropped rather than indexed, since nothing will claim it.
    // Session rows are keyed by dataset.sessionId exactly as before -- group
    // headings share the same map under a `group:`-prefixed key so the one
    // diff/reposition pass below covers both, but that reuse is cosmetic
    // only: it never touches how a session row itself is found, built or
    // patched.
    const existingItems = new Map();
    for (const child of Array.from(elements.sessionLibrary.children)) {
      if (child.dataset.sessionId) existingItems.set(child.dataset.sessionId, child);
      else if (child.dataset.groupKey) existingItems.set(`group:${child.dataset.groupKey}`, child);
      else child.remove();
    }

    let previousItem = null;
    for (const renderItem of renderItems) {
      let item = existingItems.get(renderItem.key);
      if (item) {
        existingItems.delete(renderItem.key);
      } else {
        item =
          renderItem.kind === "group"
            ? buildGroupHeadingRow(renderItem.key.slice("group:".length))
            : buildSessionRow(renderItem.session);
      }
      if (renderItem.kind === "group") {
        updateGroupHeadingRow(item, renderItem.label);
      } else {
        updateSessionRow(item, renderItem.session);
      }
      const expectedNext = previousItem
        ? previousItem.nextElementSibling
        : elements.sessionLibrary.firstElementChild;
      if (expectedNext !== item) {
        if (previousItem) previousItem.after(item);
        else elements.sessionLibrary.prepend(item);
      }
      previousItem = item;
    }

    // Whatever is left in the map fell out of state.sessions -- filtered out
    // by a search, or actually removed -- and does not belong on screen.
    for (const stale of existingItems.values()) stale.remove();
  }

  function mergeSession(session) {
    const index = state.sessions.findIndex((item) => item.uuid_code === session.uuid_code);
    if (index === -1) {
      state.sessions.unshift(session);
    } else {
      state.sessions[index] = { ...state.sessions[index], ...session };
    }
    sortSessions();
  }

  async function loadSessions({ reset = false } = {}) {
    // Three ways the sentinel can ask twice for the same page: it stays in view
    // while the request is in flight, the scroll jitters, or a filter change
    // races it. All three land here.
    if (state.sessionsLoading && !reset) return;
    if (!reset && !state.nextCursor) return;
    const requestId = ++state.sessionsRequestId;
    const cursor = reset ? null : state.nextCursor;
    const params = new URLSearchParams();
    if (cursor) params.set("cursor", cursor);
    if (state.searchQuery) params.set("q", state.searchQuery);
    state.sessionsLoading = true;
    if (reset) state.sessions = [];
    renderSessions();
    try {
      const suffix = params.toString() ? `?${params.toString()}` : "";
      const page = await localFetch(`/api/sessions${suffix}`);
      if (requestId !== state.sessionsRequestId) return;
      const sessions = reset ? page.sessions : [...state.sessions, ...page.sessions];
      if (state.selectedSession) {
        state.sessions = sessions.map((session) =>
          session.uuid_code === state.selectedSession.uuid_code && !session.title
            ? { ...session, title: state.selectedSession.title }
            : session,
        );
      } else {
        state.sessions = sessions;
      }
      sortSessions();
      state.nextCursor = page.next_cursor;
      if (state.selectedSession) {
        const refreshed = state.sessions.find(
          (session) => session.uuid_code === state.selectedSession.uuid_code,
        );
        if (refreshed) {
          state.selectedSession = {
            ...state.selectedSession,
            ...refreshed,
            title: refreshed.title || state.selectedSession.title,
          };
        }
      }
    } finally {
      if (requestId === state.sessionsRequestId) {
        state.sessionsLoading = false;
        renderSessions();
      }
    }
  }

  /**
   * Load the next page when the end of the list comes into view.
   *
   * Observed once, at boot, on an element that lives outside the list body --
   * `renderSessions` replaces the list wholesale, and re-observing a fresh node
   * on every render would be a subscription leak dressed as a feature.
   */
  function watchSessionsSentinel() {
    if (!("IntersectionObserver" in window)) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        loadSessions().catch(reportError);
      },
      // Ahead of the fold, so the next page is usually already there by the
      // time the user reaches the bottom.
      { root: elements.sessionHistoryScroll, rootMargin: "200px" },
    );
    observer.observe(elements.sessionsSentinel);
  }

  /**
   * Keep the transcript clear of the floating capture panel.
   *
   * The clearance used to be a hand-picked padding, which stopped matching the
   * moment the panel changed height at a breakpoint. Measuring it means the last
   * line is never the one hidden behind the controls.
   */
  function watchCaptureDockHeight() {
    const apply = () => {
      const height = elements.captureDock.getBoundingClientRect().height;
      if (height) document.documentElement.style.setProperty("--capture-dock-height", `${height}px`);
    };
    if ("ResizeObserver" in window) {
      new ResizeObserver(apply).observe(elements.captureDock);
      return;
    }
    apply();
    window.addEventListener("resize", apply);
  }

  function scheduleSessionSearch() {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => {
      const query = elements.sessionSearchInput.value.trim();
      if (query === state.searchQuery) return;
      state.searchQuery = query;
      state.nextCursor = null;
      loadSessions({ reset: true }).catch(reportError);
    }, 250);
  }

  // ---------------------------------------------------------------------- capture

  const CONNECTION_LABELS = {
    idle: "Pronto",
    starting: "Iniciando",
    streaming: "Transmitindo",
    reconnecting: "Reconectando",
    stopped: "Parado",
    failed: "Falha",
    device_selection_required: "Dispositivo necessário",
  };
  // Only the tone varies, so only the tone is swapped -- assigning a whole
  // className here would silently drop whatever `hidden` renderView had put on
  // the badge, and leave the two functions depending on their call order.
  const CONNECTION_BADGE_TONES = {
    streaming: "badge-success",
    starting: "badge-success",
    reconnecting: "badge-warning",
    failed: "badge-error",
    device_selection_required: "badge-error",
  };
  const BADGE_TONES = ["badge-success", "badge-warning", "badge-error"];

  function isCaptureActive() {
    return ["starting", "streaming", "reconnecting"].includes(state.connectionState);
  }

  function renderCaptureDock() {
    const active = isCaptureActive();
    elements.captureDock.dataset.captureState = state.connectionState;
    elements.captureToggleButton.classList.toggle("btn-error", active);
    elements.captureToggleButton.classList.toggle("btn-primary", !active);
    elements.captureToggleButton.setAttribute(
      "aria-label",
      active ? "Parar captura" : "Iniciar captura",
    );
    elements.captureToggleButton.title = active ? "Parar captura" : "Iniciar captura";
    elements.captureToggleButton.replaceChildren(
      icon(active ? "stop-fill" : "play-fill", "text-2xl"),
    );
    captureMotion.setState(state.connectionState);
  }

  // What renderConnectionState last fired a toast for. A render happens far
  // more often than the connection state actually changes -- the level
  // stream alone can trigger several while an outage is ongoing -- so
  // notifying unconditionally on every render re-fired "Reconectando..."
  // roughly every 15s for as long as the outage lasted. Comparing against
  // this turns that into exactly one toast per transition into the state.
  let lastNotifiedConnectionState = null;

  function renderConnectionState() {
    elements.captureIndicatorLabel.textContent =
      CONNECTION_LABELS[state.connectionState] || CONNECTION_LABELS.idle;
    const tone = CONNECTION_BADGE_TONES[state.connectionState];
    elements.captureIndicator.classList.remove(...BADGE_TONES);
    if (tone) elements.captureIndicator.classList.add(tone);
    if (state.connectionState === "device_selection_required") {
      elements.deviceRequired.classList.remove("hidden");
    }
    if (state.connectionState !== lastNotifiedConnectionState) {
      if (state.connectionState === "reconnecting") {
        showNotification("Reconectando à transcrição…", "warning");
      }
      if (state.connectionState === "device_selection_required") {
        showNotification("Selecione os dispositivos antes de continuar.", "error");
      }
      lastNotifiedConnectionState = state.connectionState;
    }
    renderCaptureDock();
    renderView();
  }

  // ------------------------------------------------------------------- transcript

  // Distance from the bottom that still counts as "following along", matching
  // the platform's chat. Wide enough that a stray wheel notch does not detach
  // the view, narrow enough that reading one message back does.
  const TRANSCRIPT_FOLLOW_THRESHOLD = 100;
  let transcriptScrollFrame = null;

  function isNearTimelineBottom() {
    const timeline = elements.transcriptTimeline;
    return (
      timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight <=
      TRANSCRIPT_FOLLOW_THRESHOLD
    );
  }

  /**
   * Scroll to the newest content, coalescing to one write per frame.
   *
   * A live capture appends on every delta; scrolling inline on each one reads
   * and writes layout several times a frame for a single visible result.
   */
  function scrollTranscriptToLatest({ force = false } = {}) {
    if (!force && !state.followTranscript) return;
    if (transcriptScrollFrame !== null) return;
    transcriptScrollFrame = window.requestAnimationFrame(() => {
      transcriptScrollFrame = null;
      elements.transcriptTimeline.scrollTop = elements.transcriptTimeline.scrollHeight;
      if (force) setFollowTranscript(true);
    });
  }

  function setFollowTranscript(following) {
    state.followTranscript = following;
    // Reaching the bottom means the user has seen everything, so the invitation
    // to jump goes away with it.
    if (following) state.transcriptHasNewContent = false;
    elements.jumpToLatestButton.classList.toggle(
      "hidden",
      following || !state.transcriptHasNewContent,
    );
  }

  function handleTranscriptScroll() {
    setFollowTranscript(isNearTimelineBottom());
  }

  /** Note content that arrived while the user was reading further up. */
  function noteTranscriptActivity() {
    if (state.followTranscript) return;
    state.transcriptHasNewContent = true;
    elements.jumpToLatestButton.classList.remove("hidden");
  }

  /**
   * Append a finalized row directly into the `role="log"` region.
   *
   * Inserted *before* `#transcriptProvisional`, not appended at the very end,
   * so a still-growing delta (a different channel, still speaking) stays
   * pinned below every finalized row instead of finalized text landing under
   * it. `insertBefore` with a `null` reference falls back to appending, so
   * this is still safe if the provisional container is ever absent.
   */
  function appendTimelineRow(row) {
    elements.transcriptTimeline.querySelector("#emptyTimeline")?.remove();
    elements.transcriptTimeline.insertBefore(row, elements.transcriptProvisional || null);
    noteTranscriptActivity();
    scrollTranscriptToLatest();
  }

  /**
   * Append (or update) a provisional row inside the `aria-live="off"` region.
   *
   * A live capture calls this on every ASR delta -- several times a second
   * while someone is speaking. Because the container is aria-live="off", none
   * of those updates are announced; the only announcement is the single
   * append `appendTimelineRow` makes into the `role="log"` region, once the
   * utterance is finalized.
   */
  function appendProvisionalRow(row) {
    elements.transcriptTimeline.querySelector("#emptyTimeline")?.remove();
    elements.transcriptProvisional.append(row);
    noteTranscriptActivity();
    scrollTranscriptToLatest();
  }

  function formatTranscriptTimestamp(offsetMs) {
    const totalSeconds = Math.max(0, Math.floor(offsetMs / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const clock = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    return hours ? `${String(hours).padStart(2, "0")}:${clock}` : clock;
  }

  function transcriptRow(entry, isDelta) {
    const row = document.createElement("article");
    row.className = isDelta
      ? "transcript-preview__entry transcript-preview__entry--provisional"
      : "transcript-preview__entry";
    row.dataset.utteranceId = entry.utterance_id;
    const avatar = document.createElement("div");
    avatar.className = "avatar avatar-placeholder shrink-0";
    const avatarFace = document.createElement("div");
    avatarFace.className =
      entry.channel === "mic"
        ? "w-9 rounded-full bg-primary/20 text-sm font-semibold text-primary"
        : "w-9 rounded-full bg-secondary/20 text-sm font-semibold text-secondary";
    avatarFace.textContent = entry.channel === "mic" ? "V" : "P";
    avatar.append(avatarFace);
    const content = document.createElement("div");
    content.className = "min-w-0 flex-1";
    const heading = document.createElement("div");
    heading.className = "mb-1 flex items-center gap-2";
    const speaker = document.createElement("strong");
    speaker.className = "transcript-preview__speaker";
    speaker.textContent = entry.channel === "mic" ? "Você" : "Participantes";
    const timestamp = document.createElement("time");
    timestamp.className = "text-xs text-base-content/50";
    timestamp.textContent = formatTranscriptTimestamp(entry.started_offset_ms);
    const text = document.createElement("p");
    text.className = "transcript-preview__text";
    text.textContent = entry.text;
    heading.append(speaker, timestamp);
    content.append(heading, text);
    row.append(avatar, content);
    return row;
  }

  function renderDelta(delta) {
    state.pendingDeltas.get(delta.utterance_id)?.remove();
    const row = transcriptRow(delta, true);
    state.pendingDeltas.set(delta.utterance_id, row);
    appendProvisionalRow(row);
  }

  /**
   * Promote a finalized segment from the provisional (aria-live="off") region
   * into the log.
   *
   * The old code did `pending.replaceWith(row)` in place, inside the log
   * itself -- correct for a one-time promotion, but `renderDelta` above was
   * doing the same remove+append *inside the log* on every partial update,
   * which is what made a growing delta re-announce repeatedly. Now the
   * pending row lives outside the log (removing it is silent), and this is
   * the only point a row is ever added to the log -- exactly one `polite`
   * announcement per finalized utterance, whether or not a delta preceded it.
   */
  function renderSegment(segment) {
    const pending = state.pendingDeltas.get(segment.utterance_id);
    const row = transcriptRow(segment, false);
    if (pending) {
      pending.remove();
      state.pendingDeltas.delete(segment.utterance_id);
    }
    appendTimelineRow(row);
  }

  function clearTimeline() {
    state.pendingDeltas.clear();
    state.segments = { cursor: null, loading: false, requestId: 0 };
    state.transcriptHasNewContent = false;
    setFollowTranscript(true);
    elements.transcriptProvisional.replaceChildren();
    elements.transcriptTimeline.replaceChildren();
    const empty = document.createElement("p");
    empty.id = "emptyTimeline";
    empty.className = "transcript-preview__empty";
    empty.textContent = "A transcrição aparecerá aqui.";
    elements.transcriptTimeline.append(empty, elements.transcriptProvisional);
  }

  function setTimelineLoading(message) {
    elements.transcriptTimeline.replaceChildren();
    const block = document.createElement("div");
    block.className = "flex flex-col items-center justify-center gap-3 py-16";
    const spinner = document.createElement("span");
    spinner.className = "loading loading-spinner loading-lg text-primary";
    const label = document.createElement("p");
    label.className = "text-sm text-base-content/60";
    label.textContent = message;
    block.append(spinner, label);
    elements.transcriptTimeline.append(block, elements.transcriptProvisional);
  }

  function renderSegmentLoadMore(session) {
    document.querySelector("#loadMoreSegments")?.remove();
    if (!state.segments.cursor) return;
    const button = document.createElement("button");
    button.id = "loadMoreSegments";
    button.type = "button";
    button.className = "btn btn-ghost btn-sm mx-auto my-2";
    button.dataset.testid = "load-more-segments";
    button.textContent = "Carregar mais da transcrição";
    button.addEventListener("click", () => {
      button.disabled = true;
      loadSegments(session).catch((error) => {
        button.disabled = false;
        reportError(error);
      });
    });
    elements.transcriptTimeline.insertBefore(button, elements.transcriptProvisional || null);
  }

  /**
   * Fetch one page of a retained session's transcript.
   *
   * One page, not all of them: walking every page up front meant a long meeting
   * fired dozens of sequential requests before the screen answered at all.
   */
  async function loadSegments(session, { first = false } = {}) {
    if (state.segments.loading) return;
    const requestId = ++state.segments.requestId;
    state.segments.loading = true;
    try {
      const cursor = state.segments.cursor;
      const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
      const page = await localFetch(
        `/api/sessions/${encodeURIComponent(session.uuid_code)}/segments${suffix}`,
      );
      if (requestId !== state.segments.requestId) return;
      if (state.selectedSession?.uuid_code !== session.uuid_code) return;
      // The first page replaces the spinner; later pages append below the rows
      // already on screen.
      if (first) {
        elements.transcriptTimeline.replaceChildren();
        elements.transcriptTimeline.append(elements.transcriptProvisional);
      }
      document.querySelector("#loadMoreSegments")?.remove();
      elements.transcriptTimeline.querySelector("#emptyTimeline")?.remove();
      for (const segment of page.segments) {
        elements.transcriptTimeline.insertBefore(transcriptRow(segment, false), elements.transcriptProvisional || null);
      }
      state.segments.cursor = page.next_cursor;
      renderSegmentLoadMore(session);
      if (!elements.transcriptTimeline.querySelector("article")) {
        const empty = document.createElement("p");
        empty.id = "emptyTimeline";
        empty.className = "transcript-preview__empty";
        empty.textContent = "Esta sessão não tem transcrição registrada.";
        elements.transcriptTimeline.insertBefore(empty, elements.transcriptProvisional || null);
      }
    } finally {
      if (requestId === state.segments.requestId) state.segments.loading = false;
    }
  }

  async function selectSession(session) {
    if (isCaptureActive() && state.selectedSession?.uuid_code !== session.uuid_code) {
      showNotification("Pare a captura antes de abrir outra sessão.", "warning");
      return;
    }
    state.selectedSession = session;
    clearTimeline();
    closeDrawer();
    renderSessions();
    renderSessionDetails();
    focusScreen(elements.mainView);
    state.segments = { cursor: null, loading: false, requestId: 0 };
    setTimelineLoading("Carregando transcrição…");
    try {
      await loadSegments(session, { first: true });
    } catch (error) {
      if (state.selectedSession?.uuid_code === session.uuid_code) clearTimeline();
      throw error;
    }
  }

  async function updateSessionMetadata(session, changes) {
    const updated = await localFetch(`/api/sessions/${encodeURIComponent(session.uuid_code)}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    });
    mergeSession(updated);
    if (state.selectedSession?.uuid_code === updated.uuid_code) {
      state.selectedSession = { ...state.selectedSession, ...updated };
    }
    renderSessions();
    renderSessionDetails();
    const message = Object.hasOwn(changes, "is_pinned")
      ? updated.is_pinned
        ? "Sessão fixada."
        : "Sessão desafixada."
      : "Sessão renomeada.";
    showNotification(message, "success");
    return updated;
  }

  function openRenameSession(session) {
    if (!elements.renameSessionModal || !elements.renameSessionInput) return;
    state.renameSession = session;
    elements.renameSessionInput.value = session.title || "";
    elements.renameSessionInput.classList.remove("input-error");
    elements.renameSessionModal.showModal();
    window.setTimeout(() => {
      elements.renameSessionInput.focus();
      elements.renameSessionInput.select();
    }, 0);
  }

  function closeRenameSession() {
    state.renameSession = null;
    elements.renameSessionModal?.close();
  }

  async function confirmRenameSession() {
    const session = state.renameSession;
    const input = elements.renameSessionInput;
    if (!session || !input) return;
    const title = input.value.trim();
    if (!title) {
      input.classList.add("input-error");
      input.focus();
      return;
    }
    input.classList.remove("input-error");
    await updateSessionMetadata(session, { title });
    closeRenameSession();
  }

  function openDeleteSession(session) {
    if (session.is_live || !elements.deleteSessionModal) return;
    state.deleteSession = session;
    elements.deleteSessionModal.showModal();
  }

  function closeDeleteSession() {
    state.deleteSession = null;
    elements.deleteSessionModal?.close();
  }

  async function confirmDeleteSession() {
    const session = state.deleteSession;
    if (!session) return;
    await localFetch(`/api/sessions/${encodeURIComponent(session.uuid_code)}`, { method: "DELETE" });
    state.sessions = state.sessions.filter((item) => item.uuid_code !== session.uuid_code);
    if (state.selectedSession?.uuid_code === session.uuid_code) {
      state.selectedSession = null;
      clearTimeline();
    }
    closeDeleteSession();
    renderSessions();
    renderSessionDetails();
    showNotification("Sessão removida do histórico.", "success");
  }

  function renderSessionDetails() {
    const session = state.selectedSession;
    // Only adopt a stored title when there is a stored session, and only
    // when the field is not the very thing being typed into -- a `status`
    // event landing mid-rename must not overwrite an in-progress edit. With
    // no session, the field holds the draft name -- generated or typed --
    // and a socket reconnect redrawing the header must not wipe that either.
    if (session && document.activeElement !== elements.sessionTitle) {
      elements.sessionTitle.value = session.title || "";
    }
    elements.renameSessionButton.disabled = !session;
    const detailText = session
      ? `Código ${session.uuid_code} · ${segmentCountLabel(session.segment_count)}`
      : "Inicie uma captura para gerar um código local.";
    // This used to render as visible header text, wide enough at a narrow
    // window to squeeze the title input down to a couple of pixels (see
    // index.html's #sessionMeta comment and visual-check.ps1's 375px
    // assertion). It is secondary detail now: a hover tooltip on the title
    // field for a mouse, and this permanently sr-only span for everyone else.
    elements.sessionMeta.textContent = detailText;
    elements.sessionTitle.title = session ? detailText : "";
    elements.copyCodeButton.disabled = !session;
  }

  async function refreshDevices() {
    stopAudioTest("Dispositivos atualizados. Inicie um novo teste para verificar o sinal.");
    try {
      const response = await localFetch("/api/devices");
      renderDevices(response.devices);
    } catch (error) {
      reportError(error);
    }
  }

  function selectedDevicePayload() {
    return {
      microphone_id: state.selectedDevices?.microphone_id || "",
      system_device_id: state.selectedDevices?.system_device_id || "",
    };
  }

  async function startSession() {
    const devices = selectedDevicePayload();
    if (!devices.microphone_id || !devices.system_device_id) {
      elements.deviceRequired.classList.remove("hidden");
      // Previously this was a silent screen change: for a screen-reader user
      // it was indistinguishable from a dead button. settingsAudioTestStatus
      // is the settings screen's one role="status" region, so writing the
      // reason there gets it announced, and focus goes straight to the
      // control that fixes it instead of the screen's own heading.
      elements.settingsAudioTestStatus.textContent =
        "Selecione um microfone e uma saída de áudio antes de iniciar a captura.";
      showSettings(elements.settingsMicrophoneSelect);
      return;
    }
    const previousConnectionState = state.connectionState;
    const currentSession = state.selectedSession;
    const path = currentSession
      ? `/api/sessions/${encodeURIComponent(currentSession.uuid_code)}/resume`
      : "/api/sessions";
    const body = currentSession ? devices : { ...devices, title: elements.sessionTitle.value };
    try {
      stopAudioTest("Teste de áudio encerrado para iniciar a captura.");
      state.connectionState = "starting";
      clearTimeline();
      renderConnectionState();
      const session = await localFetch(path, { method: "POST", body: JSON.stringify(body) });
      state.selectedSession = session;
      mergeSession(session);
      renderSessions();
      renderSessionDetails();
    } catch (error) {
      state.connectionState = previousConnectionState;
      renderConnectionState();
      reportError(error);
    }
  }

  async function stopSession() {
    try {
      await localFetch("/api/sessions/stop", { method: "POST" });
      state.connectionState = "stopped";
      renderConnectionState();
      showNotification("Captura encerrada.", "success");
    } catch (error) {
      reportError(error);
    }
  }

  function toggleCapture() {
    if (isCaptureActive()) {
      return stopSession();
    }
    return startSession();
  }

  async function saveSessionTitle() {
    const session = state.selectedSession;
    if (!session) return;
    const title = elements.sessionTitle.value.trim();
    if (title === session.title) return;
    if (!title) {
      // Same rule the rename dialog enforces: a session with no name is one the
      // user cannot find again.
      elements.sessionTitle.value = session.title || "";
      showNotification("A sessão precisa de um nome.", "warning");
      return;
    }
    await updateSessionMetadata(session, { title });
  }

  async function copySessionCode() {
    if (!state.selectedSession) return;
    try {
      await navigator.clipboard.writeText(state.selectedSession.uuid_code);
      showNotification("Código copiado.", "success");
    } catch {
      showNotification("Não foi possível copiar o código.", "error");
    }
  }

  /**
   * Start a draft session, already named.
   *
   * The row itself is still born when the capture starts -- a listening session
   * is what the backend opens on the audio handshake, and an empty one would sit
   * against the one-active-session constraint and under the expiry sweep. What
   * is created here is the title, so the name the user sees is the name that
   * gets persisted, and a meeting nobody renames is still findable later.
   */
  async function prepareNewSession() {
    if (isCaptureActive()) {
      showNotification("Pare a captura antes de iniciar uma nova sessão.", "warning");
      return;
    }
    showTranscript();
    closeDrawer();
    state.selectedSession = null;
    state.connectionState = "idle";
    clearTimeline();
    renderSessions();
    renderSessionDetails();
    renderConnectionState();
    elements.sessionTitle.value = "";
    elements.sessionTitle.focus();
    try {
      const suggestion = await localFetch("/api/session-name");
      // Only if the field is still the untouched draft: the request is quick,
      // but not quicker than someone who starts typing straight away.
      if (!elements.sessionTitle.value) elements.sessionTitle.value = suggestion.title;
    } catch {
      // No notification: the capture names the session server-side when the
      // title arrives empty, so nothing is actually lost here.
    }
  }

  // ---------------------------------------------------------------------- events

  function applyBootstrap(bootstrap) {
    const wasAuthenticated = state.authenticated;
    state.authenticated = bootstrap.authenticated;
    if (!state.authenticated) closeAudioLevelStream();
    state.selectedDevices = bootstrap.selected_devices;
    state.connectionState = bootstrap.state;
    state.selectedSession = bootstrap.session;
    if (state.selectedSession) mergeSession(state.selectedSession);
    renderDevices(bootstrap.devices || []);
    renderView();
    renderSessions();
    renderSessionDetails();
    renderConnectionState();
    // Only the login -> capture transition, not every reconnect bootstrap: a
    // reconnect resends the same message to an already-authenticated window,
    // and yanking focus back to the transcript heading mid-read would be its
    // own regression.
    if (!wasAuthenticated && state.authenticated) focusScreen(elements.mainView);
    if (!state.authenticated) {
      clearTimeline();
      return;
    }
    connectAudioLevels();
    // A reconnect re-sends the bootstrap; reloading the list every time would
    // throw away the user's place in it for nothing.
    if (!wasAuthenticated || !state.sessions.length) {
      if (!state.selectedSession) clearTimeline();
      loadSessions({ reset: true }).catch(reportError);
    }
  }

  function handleEvent(event) {
    if (event.type === "bootstrap") {
      applyBootstrap(event.bootstrap);
    } else if (event.type === "delta" && event.delta) {
      renderDelta(event.delta);
    } else if (event.type === "segment" && event.segment) {
      renderSegment(event.segment);
    } else if (event.type === "status" && event.state) {
      state.connectionState = event.state;
      if (event.session) {
        state.selectedSession = event.session;
        mergeSession(event.session);
      }
      renderSessions();
      renderSessionDetails();
      renderConnectionState();
    } else if (event.type === "session" && event.session) {
      state.selectedSession = event.session;
      mergeSession(event.session);
      renderSessions();
      renderSessionDetails();
    } else if ((event.type === "warning" || event.type === "error") && event.message) {
      showNotification(event.message, event.type === "error" ? "error" : "warning");
    }
  }

  const EVENT_RETRY_BASE_MS = 500;
  const EVENT_RETRY_MAX_MS = 15000;

  function connectEvents() {
    state.eventSocket?.close();
    const socket = new WebSocket(socketUrl("/api/events"));
    state.eventSocket = socket;
    socket.addEventListener("open", () => {
      state.eventRetryDelay = 0;
    });
    socket.addEventListener("message", (message) => {
      try {
        handleEvent(JSON.parse(message.data));
      } catch {
        showNotification("Não foi possível processar uma atualização local.", "error");
      }
    });
    socket.addEventListener("close", () => {
      if (state.eventSocket !== socket) return;
      state.eventSocket = null;
      // The server closes the socket right after an unauthenticated bootstrap;
      // there is nothing to come back for until a login succeeds.
      if (!state.authenticated) return;
      state.connectionState = "reconnecting";
      renderConnectionState();
      // Backoff, not a fixed second: a server that is down should not be asked
      // once a second for as long as the window stays open.
      state.eventRetryDelay = Math.min(
        state.eventRetryDelay ? state.eventRetryDelay * 2 : EVENT_RETRY_BASE_MS,
        EVENT_RETRY_MAX_MS,
      );
      window.setTimeout(() => {
        if (state.eventSocket === null) connectEvents();
      }, state.eventRetryDelay);
    });
  }

  async function signOut() {
    stopAudioTest("Teste de áudio encerrado.");
    closeAudioLevelStream();
    state.authenticated = false;
    state.eventSocket?.close();
    state.eventSocket = null;
    try {
      await localFetch("/api/login", { method: "DELETE" });
    } finally {
      state.sessionsRequestId += 1;
      state.sessions = [];
      state.nextCursor = null;
      state.searchQuery = "";
      elements.sessionSearchInput.value = "";
      state.sessionsLoading = false;
      state.selectedSession = null;
      state.selectedDevices = null;
      state.connectionState = "idle";
      state.activeView = "transcript";
      clearTimeline();
      renderView();
      renderSessions();
      renderSessionDetails();
      renderConnectionState();
      closeNotifications();
      // The socket is already gone; reopen it whatever happened. If the request
      // failed and the credential survived, the fresh bootstrap says so instead
      // of leaving the window with no feed at all.
      connectEvents();
    }
  }

  // -------------------------------------------------------------------- listeners

  elements.loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = elements.tokenInput.value.trim();
    elements.tokenInput.value = "";
    setLoginError();
    try {
      await localFetch("/api/login", { method: "POST", body: JSON.stringify({ token }) });
      closeNotifications();
      // The socket answers with the authenticated bootstrap; there is no second
      // route to ask.
      connectEvents();
    } catch (error) {
      // "Authentication is required." is the wrong-token case, common enough to
      // deserve its own wording; anything else routes through the same lookup
      // reportError uses so no other API detail reaches the screen in English.
      setLoginError(
        error.message === "Authentication is required."
          ? "Token inválido."
          : translateApiMessage(error.message),
      );
      elements.tokenInput.focus();
    }
  });
  // A same-page anchor click into a non-focusable target moves neither
  // document.activeElement nor a screen reader's cursor -- the exact silence
  // focusScreen exists to close everywhere else. #mainView carries no
  // tabindex of its own, so the jump is done here instead of left to native
  // anchor navigation, reusing the same focusScreen entry point as every
  // other transition.
  elements.skipLink?.addEventListener("click", (event) => {
    event.preventDefault();
    focusScreen(elements.mainView);
  });
  elements.settingsButton.addEventListener("click", showSettings);
  elements.backToTranscriptButton.addEventListener("click", showTranscript);
  elements.logoutButton.addEventListener("click", () => signOut().catch(reportError));
  elements.newSessionButton.addEventListener("click", () => {
    prepareNewSession().catch(reportError);
  });
  elements.sessionSearchInput.addEventListener("input", scheduleSessionSearch);
  elements.refreshDevicesButton.addEventListener("click", showSettings);
  elements.settingsRefreshDevicesButton.addEventListener("click", refreshDevices);
  elements.settingsForm.addEventListener("submit", (event) => {
    saveSettings(event).catch(reportError);
  });
  elements.resetSettingsButton.addEventListener("click", () => {
    resetSettings().catch(reportError);
  });
  elements.settingsAudioTestButton.addEventListener("click", () => {
    if (state.audioTestActive) {
      stopAudioTest();
    } else {
      startAudioTest();
    }
  });
  elements.proxyEnabled.addEventListener("change", () => {
    updateProxyFieldsVisibility();
  });
  elements.proxyTestButton.addEventListener("click", () => {
    testProxyConnection().catch(reportError);
  });
  elements.themeLightOption.addEventListener("change", () => {
    if (elements.themeLightOption.checked) setTheme("light");
  });
  elements.themeDarkOption.addEventListener("change", () => {
    if (elements.themeDarkOption.checked) setTheme("dark");
  });
  elements.settingsMicrophoneSelect.addEventListener("change", () => {
    state.pendingDevices.microphone_id = elements.settingsMicrophoneSelect.value;
    syncDeviceSelectTitle(elements.settingsMicrophoneSelect);
    if (state.audioTestActive) {
      stopAudioTest("Microfone alterado. Inicie o teste novamente para verificar o novo sinal.");
    }
  });
  elements.settingsSystemDeviceSelect.addEventListener("change", () => {
    state.pendingDevices.system_device_id = elements.settingsSystemDeviceSelect.value;
    syncDeviceSelectTitle(elements.settingsSystemDeviceSelect);
    if (state.audioTestActive) {
      stopAudioTest("Saída alterada. Inicie o teste novamente para verificar o novo sinal.");
    }
  });
  elements.captureToggleButton.addEventListener("click", () => {
    toggleCapture().catch(reportError);
  });
  elements.sessionTitle.addEventListener("change", () => {
    saveSessionTitle().catch((error) => {
      renderSessionDetails();
      reportError(error);
    });
  });
  // Inline editing with the same two keys the rename dialog answers to. The
  // value at focus time is what Escape restores, so cancelling works on a draft
  // name that has no stored version to fall back on.
  elements.sessionTitle.addEventListener("focus", (event) => {
    event.target.dataset.previous = event.target.value;
  });
  elements.sessionTitle.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      event.target.blur();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.target.value = event.target.dataset.previous ?? "";
      event.target.blur();
    }
  });
  elements.renameSessionCancel.addEventListener("click", closeRenameSession);
  elements.renameSessionConfirm.addEventListener("click", () => {
    confirmRenameSession().catch(reportError);
  });
  elements.renameSessionInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      confirmRenameSession().catch(reportError);
    }
  });
  elements.renameSessionModal.addEventListener("close", () => {
    state.renameSession = null;
  });
  elements.deleteSessionCancel.addEventListener("click", closeDeleteSession);
  elements.deleteSessionConfirm.addEventListener("click", () => {
    confirmDeleteSession().catch(reportError);
  });
  elements.deleteSessionModal.addEventListener("close", () => {
    state.deleteSession = null;
  });
  elements.copyCodeButton.addEventListener("click", copySessionCode);
  elements.renameSessionButton.addEventListener("click", () => {
    if (state.selectedSession) openRenameSession(state.selectedSession);
  });
  elements.transcriptTimeline.addEventListener("scroll", handleTranscriptScroll, {
    passive: true,
  });
  elements.jumpToLatestButton.addEventListener("click", () => {
    scrollTranscriptToLatest({ force: true });
  });
  window.addEventListener("pagehide", closeAudioLevelStream);
  window.addEventListener("pageshow", () => {
    if (state.authenticated && !state.audioLevelSource) connectAudioLevels();
  });

  clearTimeline();
  renderView();
  renderSessions();
  watchSessionsSentinel();
  watchCaptureDockHeight();
  connectEvents();
})();
