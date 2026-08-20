(() => {
  "use strict";

  const elements = {
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
    loadMoreButton: document.querySelector("#loadMoreButton"),
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
    captureIndicator: document.querySelector("#captureIndicator"),
    captureIndicatorLabel: document.querySelector("#captureIndicatorLabel"),
    themeToggleButton: document.querySelector("#themeToggleButton"),
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
    resetSettingsButton: document.querySelector("#resetSettingsButton"),
    sessionTitle: document.querySelector("#sessionTitle"),
    sessionMeta: document.querySelector("#sessionMeta"),
    refreshDevicesButton: document.querySelector("#refreshDevicesButton"),
    deviceRequired: document.querySelector("#deviceRequired"),
    captureToggleButton: document.querySelector("#captureToggleButton"),
    copyCodeButton: document.querySelector("#copyCodeButton"),
    openBroccoliLink: document.querySelector("#openBroccoliLink"),
    captureDock: document.querySelector("#captureDock"),
    microphoneHistogram: document.querySelector("#microphoneHistogram"),
    systemHistogram: document.querySelector("#systemHistogram"),
    transcriptTimeline: document.querySelector("#transcriptTimeline"),
    drawerToggle: document.querySelector("#drawer-toggle"),
  };

  const SETTINGS_STORAGE_KEY = "broccoli-desktop-settings";
  // Same key and same values the platform writes, so the two surfaces agree on
  // what "dark" means and a theme picked in one reads naturally in the other.
  const THEME_STORAGE_KEY = "theme";
  const defaultSettings = {
    proxyEnabled: false,
    proxyHost: "",
    proxyPort: "",
    proxyUsername: "",
    proxyPassword: "",
  };

  function loadSettings() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(SETTINGS_STORAGE_KEY) || "null");
      if (!saved || typeof saved !== "object") return { ...defaultSettings };
      return { ...defaultSettings, ...saved, proxyEnabled: Boolean(saved.proxyEnabled) };
    } catch {
      return { ...defaultSettings };
    }
  }

  function persistSettings() {
    try {
      window.localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(state.settings));
    } catch {
      // Local preferences are best-effort in restricted browser contexts.
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
    const icon = elements.themeToggleButton?.querySelector("i");
    if (icon) icon.className = resolved === "dark" ? "bi bi-moon" : "bi bi-sun";
    if (elements.themeLightOption) elements.themeLightOption.checked = resolved === "light";
    if (elements.themeDarkOption) elements.themeDarkOption.checked = resolved === "dark";
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
    pendingDeltas: new Map(),
    eventSocket: null,
    eventRetryDelay: 0,
    sessionsLoading: false,
    sessionsRequestId: 0,
    segments: { cursor: null, loading: false, requestId: 0 },
    activeView: "transcript",
    settings: loadSettings(),
    theme: loadTheme(),
    audioTestActive: false,
    audioMeterBars: { microphone: [], system: [] },
    audioLevelSource: null,
    audioLevelRetry: null,
    renameSession: null,
    deleteSession: null,
  };

  applyTheme(state.theme);

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

  class CaptureMotion {
    constructor({ microphoneHistogram, systemHistogram }) {
      this.microphoneHistogram = microphoneHistogram;
      this.systemHistogram = systemHistogram;
      this.histogramBars = { microphone: [], system: [] };
      this.samples = { microphone: [], system: [] };
      this.captureState = "idle";
      this.signalActive = false;
    }

    mount() {
      this.histogramBars.microphone = this.createBars(
        this.microphoneHistogram,
        30,
        "capture-histogram__bar",
      );
      this.histogramBars.system = this.createBars(
        this.systemHistogram,
        30,
        "capture-histogram__bar",
      );
      this.samples.microphone = Array(this.histogramBars.microphone.length).fill(0);
      this.samples.system = Array(this.histogramBars.system.length).fill(0);
      this.paint();
    }

    createBars(container, count, className) {
      if (!container) return [];
      container.replaceChildren();
      const bars = [];
      for (let index = 0; index < count; index += 1) {
        const bar = document.createElement("span");
        bar.className = className;
        bar.setAttribute("aria-hidden", "true");
        container.append(bar);
        bars.push(bar);
      }
      return bars;
    }

    setState(connectionState) {
      this.captureState = connectionState;
      this.paint();
    }

    isActive() {
      return ["starting", "streaming", "reconnecting"].includes(this.captureState);
    }

    setLevels(snapshot = {}) {
      const channels = { microphone: snapshot.microphone, system: snapshot.system };
      this.signalActive = Boolean(snapshot.active);
      Object.entries(channels).forEach(([channel, measurement]) => {
        const level = Math.max(0, Math.min(1, Number(measurement?.level) || 0));
        const peak = Math.max(level, Math.min(1, Number(measurement?.peak) || 0));
        const samples = this.samples[channel];
        const bars = this.histogramBars[channel];
        if (!bars.length) return;
        samples.push({ level, peak });
        while (samples.length > bars.length) samples.shift();
      });
      this.paint();
    }

    paint() {
      const active = this.signalActive && this.isActive();
      Object.entries(this.histogramBars).forEach(([channel, bars]) => {
        const samples = this.samples[channel];
        const channelOffset = channel === "microphone" ? 0 : 1.8;
        const peakIndex = samples.reduce(
          (best, sample, index) => (sample.peak > (samples[best]?.peak || 0) ? index : best),
          0,
        );
        bars.forEach((bar, index) => {
          const sample = samples[index] || { level: 0, peak: 0 };
          const envelope = Math.min(1, Math.sqrt(sample.level) * 1.6);
          const profile = 0.82 + 0.18 * Math.sin(index * 1.37 + channelOffset);
          const height = active ? 4 + envelope * 88 * profile : 4;
          bar.style.setProperty("--histogram-height", `${height.toFixed(1)}%`);
          bar.classList.toggle("is-peak", active && index === peakIndex && sample.peak > 0.04);
        });
      });
    }
  }

  const captureMotion = new CaptureMotion(elements);
  captureMotion.mount();

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
    const query = deviceTest ? `?${new URLSearchParams(deviceTest)}` : "";
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
      let detail = "Não foi possível iniciar o teste de áudio.";
      try {
        detail = JSON.parse(event.data).detail || detail;
      } catch {
        // Keep the generic message; the stream is ending either way.
      }
      finishAudioTest(detail);
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

  async function localFetch(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (response.ok) {
      return response.status === 204 ? null : response.json();
    }
    if (response.status === 401) closeAudioLevelStream();
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "Não foi possível concluir esta ação.");
  }

  function reportError(error) {
    showNotification(error.message, "error");
  }

  function setLoginError(message = "") {
    elements.loginError.textContent = message;
    elements.loginError.classList.toggle("hidden", !message);
  }

  // ------------------------------------------------------------------------ views

  function renderView() {
    const settingsOpen = state.authenticated && state.activeView === "settings";
    elements.loginView.classList.toggle("hidden", state.authenticated);
    elements.mainView.classList.toggle("hidden", !state.authenticated || settingsOpen);
    elements.settingsView.classList.toggle("hidden", !settingsOpen);
    elements.sidebarFooter.classList.toggle("hidden", !state.authenticated);
    elements.newSessionButton.classList.toggle("hidden", !state.authenticated);
    elements.sessionHistorySection.classList.toggle("hidden", !state.authenticated);
    elements.transcriptHeaderContext.classList.toggle("hidden", settingsOpen || !state.authenticated);
    elements.settingsHeaderTitle.classList.toggle("hidden", !settingsOpen);
    elements.backToTranscriptButton.classList.toggle("hidden", !settingsOpen);
    // The capture badge and the code button describe an open session; on the
    // login screen and in settings the header keeps only the theme toggle and
    // the link out to the platform.
    const sessionActionsVisible = state.authenticated && !settingsOpen;
    elements.captureIndicator.classList.toggle("hidden", !sessionActionsVisible);
    elements.copyCodeButton.classList.toggle("hidden", !sessionActionsVisible);
    elements.openBroccoliLink.classList.toggle("hidden", !state.authenticated);
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
    elements.proxyEnabled.checked = state.settings.proxyEnabled;
    elements.proxyHost.value = state.settings.proxyHost;
    elements.proxyPort.value = state.settings.proxyPort;
    elements.proxyUsername.value = state.settings.proxyUsername;
    elements.proxyPassword.value = state.settings.proxyPassword;
    updateProxyFieldsVisibility();
    renderAudioTestControls();
  }

  function showSettings() {
    if (!state.authenticated) return;
    state.activeView = "settings";
    closeDrawer();
    renderView();
    renderSettings();
  }

  function showTranscript() {
    state.activeView = "transcript";
    renderView();
    stopAudioTest();
  }

  function collectSettings() {
    return {
      ...state.settings,
      proxyEnabled: elements.proxyEnabled.checked,
      proxyHost: elements.proxyHost.value.trim(),
      proxyPort: elements.proxyPort.value.trim(),
      proxyUsername: elements.proxyUsername.value.trim(),
      proxyPassword: elements.proxyPassword.value,
    };
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
    const nextSettings = collectSettings();
    setTheme(elements.themeLightOption.checked ? "light" : "dark");
    try {
      await saveDeviceSelection();
    } catch (error) {
      reportError(error);
      return;
    }
    state.settings = nextSettings;
    persistSettings();
    updateDeviceRequirement();
    renderSettings();
    showNotification("Configurações salvas nesta máquina.", "success");
  }

  async function resetSettings() {
    state.settings = { ...defaultSettings };
    setTheme("dark");
    elements.settingsMicrophoneSelect.value = "";
    elements.settingsSystemDeviceSelect.value = "";
    state.pendingDevices = { microphone_id: "", system_device_id: "" };
    persistSettings();
    stopAudioTest("Configurações restauradas. O teste de áudio foi encerrado.");
    await localFetch("/api/devices/selection", { method: "DELETE" });
    state.selectedDevices = null;
    renderSettings();
    updateDeviceRequirement();
    showNotification("Configurações restauradas.", "info");
  }

  // ------------------------------------------------------------------- session list

  function sessionLabel(session) {
    return session.title || session.device_label || session.uuid_code;
  }

  function sortSessions() {
    state.sessions.sort((left, right) => {
      if (Boolean(left.is_pinned) !== Boolean(right.is_pinned)) {
        return left.is_pinned ? -1 : 1;
      }
      if (left.is_pinned && left.pinned_at !== right.pinned_at) {
        return String(right.pinned_at || "").localeCompare(String(left.pinned_at || ""));
      }
      if (left.started_at !== right.started_at) {
        return String(right.started_at || "").localeCompare(String(left.started_at || ""));
      }
      return String(left.uuid_code).localeCompare(String(right.uuid_code));
    });
  }

  function icon(name, extraClasses = "") {
    const element = document.createElement("i");
    element.className = `bi bi-${name}${extraClasses ? ` ${extraClasses}` : ""}`;
    element.setAttribute("aria-hidden", "true");
    return element;
  }

  function pinIcon() {
    const element = icon("pin-angle-fill", "session-row-pin shrink-0 text-primary text-xs");
    element.removeAttribute("aria-hidden");
    element.setAttribute("aria-label", "Sessão fixada");
    element.title = "Sessão fixada";
    return element;
  }

  function closeSessionMenu(trigger) {
    trigger?.blur();
  }

  function addSessionMenuAction(menu, { label, iconName, className = "", disabled = false, onClick }) {
    const item = document.createElement("li");
    const action = document.createElement("button");
    action.type = "button";
    action.className = `session-menu-action ${className}`.trim();
    action.disabled = disabled;
    const text = document.createElement("span");
    text.textContent = label;
    action.append(icon(iconName), text);
    action.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!disabled) onClick();
    });
    item.append(action);
    menu.append(item);
  }

  function sessionActionMenu(session) {
    const dropdown = document.createElement("div");
    dropdown.className = "dropdown dropdown-end session-row-actions";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "session-menu-trigger btn btn-sm h-[30px] w-[30px] min-h-0 bg-transparent";
    trigger.setAttribute("aria-label", `Opções para ${sessionLabel(session)}`);
    trigger.title = "Opções da sessão";
    trigger.append(icon("three-dots"));
    const menu = document.createElement("ul");
    menu.className = "dropdown-content menu z-[1] w-36 rounded-box bg-base-100 p-1 shadow";
    menu.tabIndex = 0;

    addSessionMenuAction(menu, {
      label: session.is_pinned ? "Desafixar" : "Fixar",
      iconName: session.is_pinned ? "pin-angle" : "pin-angle-fill",
      onClick: () => {
        closeSessionMenu(trigger);
        updateSessionMetadata(session, { is_pinned: !session.is_pinned }).catch(reportError);
      },
    });
    addSessionMenuAction(menu, {
      label: "Renomear",
      iconName: "pencil",
      onClick: () => {
        closeSessionMenu(trigger);
        openRenameSession(session);
      },
    });
    addSessionMenuAction(menu, {
      label: session.is_live ? "Excluir sessão ativa" : "Excluir",
      iconName: "trash",
      className: "text-error",
      disabled: session.is_live,
      onClick: () => {
        closeSessionMenu(trigger);
        openDeleteSession(session);
      },
    });
    if (session.is_live) {
      menu.lastElementChild
        ?.querySelector("button")
        ?.setAttribute("title", "Pare a captura antes de excluir esta sessão.");
    }

    dropdown.append(trigger, menu);
    const preventSessionSelection = (event) => event.stopPropagation();
    dropdown.addEventListener("pointerdown", preventSessionSelection);
    dropdown.addEventListener("mousedown", preventSessionSelection);
    dropdown.addEventListener("mouseup", preventSessionSelection);
    dropdown.addEventListener("click", preventSessionSelection);
    return dropdown;
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

  function renderSessions() {
    elements.sessionLibrary.replaceChildren();
    elements.loadMoreButton.disabled = state.sessionsLoading || !state.nextCursor;
    elements.loadMoreButton.classList.toggle("hidden", !state.nextCursor);

    if (!state.sessions.length) {
      elements.sessionLibrary.append(
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
    for (const session of state.sessions) {
      const item = document.createElement("li");
      item.className =
        "session-library-item flex min-w-0 items-center rounded-md text-md transition-all duration-200 hover:bg-base-300/50";
      item.classList.toggle("is-active", state.selectedSession?.uuid_code === session.uuid_code);
      const content = document.createElement("div");
      content.className = "session-row-content flex w-full min-w-0 items-center";
      content.dataset.testid = `session-row-${session.uuid_code}`;
      content.setAttribute("role", "button");
      content.tabIndex = 0;
      const label = sessionLabel(session);
      content.setAttribute("aria-label", `Abrir sessão ${label}`);
      const rowContent = document.createElement("span");
      rowContent.className = "session-title-wrap flex min-w-0 grow flex-col overflow-hidden";
      const titleLine = document.createElement("span");
      titleLine.className = "flex min-w-0 items-center gap-1";
      const title = document.createElement("span");
      title.className = "block min-w-0 whitespace-nowrap overflow-hidden text-ellipsis";
      title.title = label;
      title.textContent = label;
      if (session.is_pinned) titleLine.append(pinIcon());
      titleLine.append(title);
      rowContent.append(titleLine);
      content.append(rowContent);
      content.addEventListener("click", () => {
        selectSession(session).catch(reportError);
      });
      content.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        selectSession(session).catch(reportError);
      });
      content.append(sessionActionMenu(session));
      item.append(content);
      elements.sessionLibrary.append(item);
    }
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

  function renderConnectionState() {
    elements.captureIndicatorLabel.textContent =
      CONNECTION_LABELS[state.connectionState] || CONNECTION_LABELS.idle;
    const tone = CONNECTION_BADGE_TONES[state.connectionState];
    elements.captureIndicator.classList.remove(...BADGE_TONES);
    if (tone) elements.captureIndicator.classList.add(tone);
    if (state.connectionState === "reconnecting") {
      showNotification("Reconectando à transcrição…", "warning");
    }
    if (state.connectionState === "device_selection_required") {
      elements.deviceRequired.classList.remove("hidden");
      showNotification("Selecione os dispositivos antes de continuar.", "error");
    }
    renderCaptureDock();
    renderView();
  }

  // ------------------------------------------------------------------- transcript

  function isNearTimelineBottom() {
    const timeline = elements.transcriptTimeline;
    return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 48;
  }

  function appendTimelineRow(row) {
    const shouldFollow = isNearTimelineBottom();
    elements.transcriptTimeline.querySelector("#emptyTimeline")?.remove();
    elements.transcriptTimeline.append(row);
    if (shouldFollow) {
      elements.transcriptTimeline.scrollTop = elements.transcriptTimeline.scrollHeight;
    }
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
    appendTimelineRow(row);
  }

  function renderSegment(segment) {
    const pending = state.pendingDeltas.get(segment.utterance_id);
    const row = transcriptRow(segment, false);
    if (pending) {
      const shouldFollow = isNearTimelineBottom();
      pending.replaceWith(row);
      state.pendingDeltas.delete(segment.utterance_id);
      if (shouldFollow) {
        elements.transcriptTimeline.scrollTop = elements.transcriptTimeline.scrollHeight;
      }
      return;
    }
    appendTimelineRow(row);
  }

  function clearTimeline() {
    state.pendingDeltas.clear();
    state.segments = { cursor: null, loading: false, requestId: 0 };
    elements.transcriptTimeline.replaceChildren();
    const empty = document.createElement("p");
    empty.id = "emptyTimeline";
    empty.className = "transcript-preview__empty";
    empty.textContent = "A transcrição aparecerá aqui.";
    elements.transcriptTimeline.append(empty);
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
    elements.transcriptTimeline.append(block);
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
    elements.transcriptTimeline.append(button);
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
      if (first) elements.transcriptTimeline.replaceChildren();
      document.querySelector("#loadMoreSegments")?.remove();
      elements.transcriptTimeline.querySelector("#emptyTimeline")?.remove();
      for (const segment of page.segments) {
        elements.transcriptTimeline.append(transcriptRow(segment, false));
      }
      state.segments.cursor = page.next_cursor;
      renderSegmentLoadMore(session);
      if (!elements.transcriptTimeline.querySelector("article")) {
        const empty = document.createElement("p");
        empty.id = "emptyTimeline";
        empty.className = "transcript-preview__empty";
        empty.textContent = "Esta sessão não tem transcrição registrada.";
        elements.transcriptTimeline.append(empty);
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
    elements.sessionTitle.value = session?.title || "";
    elements.sessionMeta.textContent = session
      ? `Código ${session.uuid_code} · ${session.segment_count} segmentos`
      : "Inicie uma captura para gerar um código local.";
    elements.sessionMeta.classList.toggle("hidden", !session);
    elements.sessionTitle.title = "";
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
      showSettings();
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

  function prepareNewSession() {
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
    elements.sessionTitle.focus();
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
    elements.openBroccoliLink.href = bootstrap.official_broccoli_url;
    renderDevices(bootstrap.devices || []);
    renderView();
    renderSessions();
    renderSessionDetails();
    renderConnectionState();
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
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${scheme}//${window.location.host}/api/events`);
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
      setLoginError(
        error.message === "Authentication is required." ? "Token inválido." : error.message,
      );
    }
  });
  elements.themeToggleButton.addEventListener("click", () => {
    setTheme(state.theme === "dark" ? "light" : "dark");
  });
  elements.settingsButton.addEventListener("click", showSettings);
  elements.backToTranscriptButton.addEventListener("click", showTranscript);
  elements.logoutButton.addEventListener("click", () => signOut().catch(reportError));
  elements.newSessionButton.addEventListener("click", prepareNewSession);
  elements.loadMoreButton.addEventListener("click", () => loadSessions().catch(reportError));
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
    state.settings.proxyEnabled = elements.proxyEnabled.checked;
    updateProxyFieldsVisibility();
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
  window.addEventListener("pagehide", closeAudioLevelStream);
  window.addEventListener("pageshow", () => {
    if (state.authenticated && !state.audioLevelSource) connectAudioLevels();
  });

  clearTimeline();
  renderView();
  renderSessions();
  connectEvents();
})();
