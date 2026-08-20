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
    statusBanner: document.querySelector("#statusBanner"),
    statusMessage: document.querySelector("#statusMessage"),
    newSessionButton: document.querySelector("#newSessionButton"),
    loadMoreButton: document.querySelector("#loadMoreButton"),
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
    transcriptHeaderActions: document.querySelector("#transcriptHeaderActions"),
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
    emptyTimeline: document.querySelector("#emptyTimeline"),
  };

  const SETTINGS_STORAGE_KEY = "broccoli-desktop-settings";
  const defaultSettings = {
    theme: "dark",
    microphoneId: "",
    systemDeviceId: "",
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
      return {
        ...defaultSettings,
        ...saved,
        theme: ["light", "dark"].includes(saved.theme) ? saved.theme : defaultSettings.theme,
        proxyEnabled: Boolean(saved.proxyEnabled),
      };
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

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme === "light" ? "broccoli-light" : "broccoli-dark";
  }

  applyTheme(loadSettings().theme);

  const state = {
    authenticated: false,
    capabilities: {
      history: false,
      remote_title: false,
      session_actions: false,
      user_resume: true,
      segment_history: false,
    },
    sessions: [],
    nextCursor: null,
    selectedSession: null,
    selectedDevices: null,
    connectionState: "idle",
    pendingDeltas: new Map(),
    eventSocket: null,
    sessionsLoading: false,
    sessionsRequestId: 0,
    segmentsLoading: false,
    activeView: "transcript",
    devices: [],
    settings: loadSettings(),
    audioTestActive: false,
    audioMeterBars: { microphone: [], system: [] },
    audioLevelSource: null,
    renameSession: null,
    deleteSession: null,
  };

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
          bar.classList.toggle(
            "is-peak",
            active && index === peakIndex && sample.peak > 0.04,
          );
        });
      });
    }
  }

  const captureMotion = new CaptureMotion(elements);
  captureMotion.mount();

  const AUDIO_METER_SEGMENTS = 18;

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
    const source = state.audioLevelSource;
    state.audioLevelSource = null;
    source?.close();
  }

  function connectAudioLevels() {
    if (!state.authenticated || !("EventSource" in window)) return;
    if (state.audioLevelSource && state.audioLevelSource.readyState !== EventSource.CLOSED) return;
    closeAudioLevelStream();
    const source = new EventSource("/api/audio-levels/stream");
    state.audioLevelSource = source;
    source.addEventListener("message", (event) => {
      try {
        handleAudioLevelSnapshot(JSON.parse(event.data));
      } catch {
        showStatus("Não foi possível processar o nível de áudio.", "error");
      }
    });
    source.addEventListener("error", () => {
      if (source.readyState === EventSource.CLOSED && state.audioLevelSource === source) {
        state.audioLevelSource = null;
      }
    });
  }

  async function startAudioTest() {
    const microphoneId = elements.settingsMicrophoneSelect.value;
    const systemDeviceId = elements.settingsSystemDeviceSelect.value;
    if (!microphoneId || !systemDeviceId) {
      finishAudioTest("Escolha um microfone e uma saída de áudio antes de testar.");
      return;
    }
    elements.settingsAudioTestButton.disabled = true;
    try {
      const levels = await localFetch("/api/audio-levels", {
        method: "POST",
        body: JSON.stringify({ microphone_id: microphoneId, system_device_id: systemDeviceId }),
      });
      state.audioTestActive = levels.active;
      renderAudioMeter("microphone", levels.microphone, levels.active);
      renderAudioMeter("system", levels.system, levels.active);
      if (!levels.active) {
        finishAudioTest("Não foi possível iniciar o teste de áudio.");
        return;
      }
      elements.settingsAudioTestStatus.textContent = "Teste em execução. Fale no microfone e reproduza um som no computador.";
      renderAudioTestControls();
    } catch (error) {
      finishAudioTest(error.message);
    } finally {
      elements.settingsAudioTestButton.disabled = false;
    }
  }

  async function stopAudioTest(message = "Teste de áudio encerrado.") {
    const wasActive = state.audioTestActive;
    finishAudioTest(message);
    if (!wasActive) return;
    try {
      await localFetch("/api/audio-levels", { method: "DELETE" });
    } catch (error) {
      showStatus(error.message, "error");
    }
  }

  mountAudioMeter(elements.settingsMicrophoneMeter, "microphone");
  mountAudioMeter(elements.settingsSystemMeter, "system");
  renderAudioMeter("microphone");
  renderAudioMeter("system");
  renderAudioTestControls();

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

  function showStatus(message, tone = "info") {
    const classes = {
      info: "alert alert-info mb-4 shadow-sm",
      success: "alert alert-success mb-4 shadow-sm",
      warning: "alert alert-warning mb-4 shadow-sm",
      error: "alert alert-error mb-4 shadow-sm",
    };
    elements.statusBanner.className = classes[tone];
    elements.statusMessage.textContent = message;
    elements.statusBanner.classList.remove("hidden");
  }

  function hideStatus() {
    elements.statusBanner.classList.add("hidden");
  }

  function setLoginError(message = "") {
    elements.loginError.textContent = message;
    elements.loginError.classList.toggle("hidden", !message);
  }

  function renderView() {
    const settingsOpen = state.authenticated && state.activeView === "settings";
    elements.loginView.classList.toggle("hidden", state.authenticated);
    elements.mainView.classList.toggle("hidden", !state.authenticated || settingsOpen);
    elements.settingsView.classList.toggle("hidden", !settingsOpen);
    elements.sidebarFooter.classList.toggle("hidden", !state.authenticated);
    elements.newSessionButton.classList.toggle("hidden", !state.authenticated);
    elements.transcriptHeaderContext.classList.toggle("hidden", settingsOpen);
    elements.settingsHeaderTitle.classList.toggle("hidden", !settingsOpen);
    elements.transcriptHeaderActions.classList.toggle("hidden", settingsOpen);
    elements.settingsButton.classList.toggle("btn-primary", settingsOpen);
    elements.settingsButton.classList.toggle("btn-ghost", !settingsOpen);
    elements.settingsButton.setAttribute("aria-current", settingsOpen ? "page" : "false");
  }

  function renderDevices(devices) {
    state.devices = devices;
    const selected = state.selectedDevices || {};
    const microphoneId = selected.microphone_id || state.settings.microphoneId;
    const systemDeviceId = selected.system_device_id || state.settings.systemDeviceId;
    renderDeviceSelect(elements.settingsMicrophoneSelect, devices.filter((device) => device.kind === "mic"), microphoneId);
    renderDeviceSelect(
      elements.settingsSystemDeviceSelect,
      devices.filter((device) => device.kind === "system"),
      systemDeviceId,
    );
    updateDeviceRequirement();
  }

  function updateDeviceRequirement() {
    const required = !state.selectedDevices?.microphone_id || !state.selectedDevices?.system_device_id;
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
    elements.themeLightOption.checked = state.settings.theme === "light";
    elements.themeDarkOption.checked = state.settings.theme === "dark";
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
    renderView();
    renderSettings();
  }

  function showTranscript() {
    state.activeView = "transcript";
    renderView();
    stopAudioTest().catch(() => {});
  }

  function collectSettings() {
    return {
      ...state.settings,
      theme: elements.themeLightOption.checked ? "light" : "dark",
      microphoneId: elements.settingsMicrophoneSelect.value,
      systemDeviceId: elements.settingsSystemDeviceSelect.value,
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
    applyTheme(nextSettings.theme);
    try {
      await saveDeviceSelection();
    } catch (error) {
      showStatus(error.message, "error");
      return;
    }
    state.settings = nextSettings;
    persistSettings();
    updateDeviceRequirement();
    renderSettings();
    showStatus("Configurações salvas nesta máquina.", "success");
  }

  async function resetSettings() {
    state.settings = { ...defaultSettings };
    applyTheme(state.settings.theme);
    elements.settingsMicrophoneSelect.value = "";
    elements.settingsSystemDeviceSelect.value = "";
    persistSettings();
    await stopAudioTest("Configurações restauradas. O teste de áudio foi encerrado.");
    await localFetch("/api/devices/selection", { method: "DELETE" });
    state.selectedDevices = null;
    renderSettings();
    updateDeviceRequirement();
    showStatus("Configurações restauradas.", "info");
  }

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

  function pinIcon() {
    const icon = document.createElement("span");
    icon.className = "session-row-pin shrink-0 text-primary";
    icon.setAttribute("aria-label", "Sessão fixada");
    icon.title = "Sessão fixada";
    icon.innerHTML = '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class="size-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m14 4 6 6-4 1-3 7-2-2-3 3-1-1 3-3-2-2 7-3Z"/></svg>';
    return icon;
  }

  function closeSessionMenu(trigger) {
    trigger?.blur();
  }

  function addSessionMenuAction(menu, { label, icon, className = "", disabled = false, onClick }) {
    const item = document.createElement("li");
    const action = document.createElement("button");
    action.type = "button";
    action.className = `session-menu-action ${className}`.trim();
    action.disabled = disabled;
    action.innerHTML = `${icon}<span>${label}</span>`;
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
    trigger.className =
      "session-menu-trigger btn btn-sm h-[30px] w-[30px] min-h-0 bg-transparent";
    trigger.setAttribute("aria-label", `Opções para ${sessionLabel(session)}`);
    trigger.title = "Opções da sessão";
    trigger.innerHTML = '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class="size-4" viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg>';
    const menu = document.createElement("ul");
    menu.className = "dropdown-content menu z-[1] w-36 rounded-box bg-base-100 p-1 shadow";
    menu.tabIndex = 0;

    addSessionMenuAction(menu, {
      label: session.is_pinned ? "Desafixar" : "Fixar",
      icon: session.is_pinned
        ? '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m14 4 6 6-4 1-3 7-2-2-3 3-1-1 3-3-2-2 7-3Z"/></svg>'
        : '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m14 4 6 6-4 1-3 7-2-2-3 3-1-1 3-3-2-2 7-3Z"/></svg>',
      onClick: () => {
        closeSessionMenu(trigger);
        updateSessionMetadata(session, { is_pinned: !session.is_pinned }).catch((error) =>
          showStatus(error.message, "error"),
        );
      },
    });
    addSessionMenuAction(menu, {
      label: "Renomear",
      icon: '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
      onClick: () => {
        closeSessionMenu(trigger);
        openRenameSession(session);
      },
    });
    addSessionMenuAction(menu, {
      label: session.is_live ? "Excluir sessão ativa" : "Excluir",
      icon: '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class="size-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>',
      className: "text-error",
      disabled: session.is_live,
      onClick: () => {
        closeSessionMenu(trigger);
        openDeleteSession(session);
      },
    });
    if (session.is_live) {
      menu.lastElementChild?.querySelector("button")?.setAttribute(
        "title",
        "Pare a captura antes de excluir esta sessão.",
      );
    }

    dropdown.append(trigger, menu);
    const preventSessionSelection = (event) => event.stopPropagation();
    dropdown.addEventListener("pointerdown", preventSessionSelection);
    dropdown.addEventListener("mousedown", preventSessionSelection);
    dropdown.addEventListener("mouseup", preventSessionSelection);
    dropdown.addEventListener("click", preventSessionSelection);
    return dropdown;
  }

  function renderSessions() {
    elements.sessionLibrary.replaceChildren();
    elements.loadMoreButton.disabled =
      !state.capabilities.history || state.sessionsLoading || !state.nextCursor;
    if (!state.capabilities.history) {
      const item = document.createElement("li");
      const unavailable = document.createElement("p");
      unavailable.className = "py-8 text-center text-sm text-base-content/60";
      unavailable.textContent = "Histórico não disponível neste backend.";
      item.append(unavailable);
      elements.sessionLibrary.append(item);
      return;
    }
    if (!state.sessions.length) {
      const item = document.createElement("li");
      if (state.sessionsLoading) {
        const loading = document.createElement("p");
        loading.className = "py-8 text-center text-sm text-base-content/60";
        loading.textContent = "Carregando sessões...";
        item.append(loading);
        elements.sessionLibrary.append(item);
        return;
      }
      const empty = document.createElement("p");
      empty.className = "py-8 text-center text-sm text-base-content/60";
      empty.textContent = "Nenhuma sessão encontrada.";
      item.append(empty);
      elements.sessionLibrary.append(item);
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
      titleLine.append(title);
      if (session.is_pinned) titleLine.append(pinIcon());
      rowContent.append(titleLine);
      content.append(rowContent);
      content.addEventListener("click", () => {
        selectSession(session).catch((error) => showStatus(error.message, "error"));
      });
      content.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        selectSession(session).catch((error) => showStatus(error.message, "error"));
      });
      if (state.capabilities.session_actions) content.append(sessionActionMenu(session));
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
    if (!state.capabilities.history || (!reset && !state.nextCursor)) return;
    const requestId = ++state.sessionsRequestId;
    const cursor = reset ? null : state.nextCursor;
    const params = new URLSearchParams();
    if (cursor) params.set("cursor", cursor);
    state.sessionsLoading = true;
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

  function isCaptureActive() {
    return ["starting", "streaming", "reconnecting"].includes(state.connectionState);
  }

  function renderCaptureDock() {
    const active = isCaptureActive();
    elements.captureDock.dataset.captureState = state.connectionState;
    elements.captureToggleButton.classList.toggle("btn-error", active);
    elements.captureToggleButton.classList.toggle("btn-primary", !active);
    elements.captureToggleButton.setAttribute("aria-label", active ? "Parar captura" : "Iniciar captura");
    elements.captureToggleButton.title = active ? "Parar captura" : "Iniciar captura";
    elements.captureToggleButton.innerHTML = active
      ? '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class="size-4" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"></rect></svg>'
      : '<svg aria-hidden="true" xmlns="http://www.w3.org/2000/svg" class="size-5" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.2v13.6c0 .8.9 1.3 1.6.8l8.1-6.8a1 1 0 0 0 0-1.6L9.6 4.4A1 1 0 0 0 8 5.2Z"></path></svg>';
    captureMotion.setState(state.connectionState);
  }

  async function selectSession(session) {
    if (isCaptureActive() && state.selectedSession?.uuid_code !== session.uuid_code) {
      showStatus("Pare a captura antes de abrir outra sessao.", "warning");
      return;
    }
    state.selectedSession = session;
    clearTimeline();
    renderSessions();
    renderSessionDetails();
    if (!state.capabilities.segment_history) return;

    state.segmentsLoading = true;
    const empty = document.getElementById("emptyTimeline");
    if (empty) empty.textContent = "Carregando transcricao...";
    try {
      let cursor = null;
      do {
        const params = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
        const page = await localFetch(
          `/api/sessions/${encodeURIComponent(session.uuid_code)}/segments${params}`,
        );
        if (state.selectedSession?.uuid_code !== session.uuid_code) return;
        for (const segment of page.segments) renderSegment(segment);
        cursor = page.next_cursor;
      } while (cursor);
    } finally {
      if (state.selectedSession?.uuid_code === session.uuid_code) {
        const empty = document.getElementById("emptyTimeline");
        if (empty && !elements.transcriptTimeline.querySelector("article")) {
          empty.textContent = "A transcrição aparecerá aqui.";
        }
      }
      state.segmentsLoading = false;
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
    showStatus(message, "success");
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
    showStatus("Sessão removida do histórico.", "success");
  }

  function renderSessionDetails() {
    const session = state.selectedSession;
    elements.sessionTitle.value = session?.title || "";
    elements.sessionMeta.textContent = session
      ? `Código ${session.uuid_code} · ${session.segment_count} segmentos`
      : "Inicie uma captura para gerar um código local.";
    elements.sessionTitle.title = "";
    elements.copyCodeButton.disabled = !session;
  }

  function renderConnectionState() {
    const label = {
      idle: "Pronto",
      starting: "Iniciando",
      streaming: "Transmitindo",
      reconnecting: "Reconectando",
      stopped: "Parado",
      failed: "Falha",
      device_selection_required: "Dispositivo necessário",
    }[state.connectionState] || "Pronto";
    if (state.connectionState === "reconnecting") {
      showStatus("Reconectando à transcrição…", "warning");
    }
    if (state.connectionState === "device_selection_required") {
      elements.deviceRequired.classList.remove("hidden");
      showStatus("Selecione os dispositivos antes de continuar.", "error");
    }
    renderCaptureDock();
  }

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
      ? "mb-8 flex max-w-2xl items-start gap-3 rounded-box border border-dashed border-base-300 bg-base-100/30 p-3"
      : "mb-8 flex max-w-2xl items-start gap-3";
    row.dataset.utteranceId = entry.utterance_id;
    const avatar = document.createElement("div");
    avatar.className = "avatar placeholder shrink-0";
    const avatarFace = document.createElement("div");
    avatarFace.className = entry.channel === "mic"
      ? "w-9 rounded-full bg-primary/20 text-sm font-semibold text-primary"
      : "w-9 rounded-full bg-secondary/20 text-sm font-semibold text-secondary";
    avatarFace.textContent = entry.channel === "mic" ? "V" : "P";
    avatar.append(avatarFace);
    const content = document.createElement("div");
    content.className = "min-w-0 flex-1";
    const heading = document.createElement("div");
    heading.className = "mb-1 flex items-center gap-2";
    const speaker = document.createElement("strong");
    speaker.className = "text-sm font-semibold";
    speaker.textContent = entry.channel === "mic" ? "Você" : "Participante";
    const timestamp = document.createElement("time");
    timestamp.className = "text-xs text-base-content/45";
    timestamp.textContent = formatTranscriptTimestamp(entry.started_offset_ms);
    const text = document.createElement("p");
    text.className = isDelta ? "text-sm italic leading-relaxed text-base-content/70" : "text-sm leading-relaxed";
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
    elements.transcriptTimeline.replaceChildren();
    const empty = document.createElement("p");
    empty.id = "emptyTimeline";
    empty.className = "flex min-h-48 items-center justify-center py-8 text-center text-sm text-base-content/60";
    empty.textContent = state.capabilities.segment_history
      ? "A transcrição aparecerá aqui."
      : "A transcrição aparecerá aqui durante esta captura.";
    elements.transcriptTimeline.append(empty);
  }

  async function refreshDevices() {
    await stopAudioTest("Dispositivos atualizados. Inicie um novo teste para verificar o sinal.");
    try {
      const response = await localFetch("/api/devices");
      renderDevices(response.devices);
    } catch (error) {
      showStatus(error.message, "error");
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
    const body = currentSession
      ? devices
      : { ...devices, title: elements.sessionTitle.value };
    try {
      await stopAudioTest("Teste de áudio encerrado para iniciar a captura.");
      state.connectionState = "starting";
      clearTimeline();
      renderConnectionState();
      const session = await localFetch(path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      state.selectedSession = session;
      mergeSession(session);
      renderSessions();
      renderSessionDetails();
    } catch (error) {
      state.connectionState = previousConnectionState;
      renderConnectionState();
      showStatus(error.message, "error");
    }
  }

  async function stopSession() {
    try {
      await localFetch("/api/sessions/stop", { method: "POST" });
      state.connectionState = "stopped";
      renderConnectionState();
      showStatus("Captura encerrada.", "success");
    } catch (error) {
      showStatus(error.message, "error");
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
      showStatus("Código copiado.", "success");
    } catch {
      showStatus("Não foi possível copiar o código.", "error");
    }
  }

  function prepareNewSession() {
    if (isCaptureActive()) {
      showStatus("Pare a captura antes de iniciar uma nova sessão.", "warning");
      return;
    }
    showTranscript();
    state.selectedSession = null;
    state.connectionState = "idle";
    clearTimeline();
    renderSessions();
    renderSessionDetails();
    renderConnectionState();
    elements.sessionTitle.focus();
  }

  function applyBootstrap(bootstrap, connectToEvents = true) {
    state.authenticated = bootstrap.authenticated;
    if (!state.authenticated) closeAudioLevelStream();
    state.capabilities = bootstrap.capabilities;
    state.selectedDevices = bootstrap.selected_devices;
    state.connectionState = bootstrap.state;
    state.selectedSession = bootstrap.session;
    if (connectToEvents || !state.authenticated) {
      state.sessions = bootstrap.sessions.sessions;
      state.nextCursor = bootstrap.sessions.next_cursor;
    }
    if (state.selectedSession) mergeSession(state.selectedSession);
    elements.openBroccoliLink.href = bootstrap.official_broccoli_url;
    renderView();
    renderSessions();
    renderSessionDetails();
    renderConnectionState();
    clearTimeline();
    if (state.authenticated) {
      refreshDevices();
      connectAudioLevels();
      if (connectToEvents) connectEvents();
      if (connectToEvents) {
        loadSessions({ reset: true }).catch((error) => showStatus(error.message, "error"));
      }
    }
  }

  function handleEvent(event) {
    if (event.type === "bootstrap") {
      applyBootstrap(event.bootstrap, false);
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
      showStatus(event.message, event.type === "error" ? "error" : "warning");
    }
  }

  function connectEvents() {
    state.eventSocket?.close();
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${scheme}//${window.location.host}/api/events`);
    state.eventSocket = socket;
    socket.addEventListener("message", (message) => {
      try {
        handleEvent(JSON.parse(message.data));
      } catch {
        showStatus("Não foi possível processar uma atualização local.", "error");
      }
    });
    socket.addEventListener("close", () => {
      if (state.authenticated && state.eventSocket === socket) {
        state.connectionState = "reconnecting";
        renderConnectionState();
        window.setTimeout(() => {
          if (state.authenticated && state.eventSocket === socket) connectEvents();
        }, 1000);
      }
    });
  }

  async function signOut() {
    await stopAudioTest("Teste de áudio encerrado.");
    closeAudioLevelStream();
    state.eventSocket?.close();
    state.eventSocket = null;
    await localFetch("/api/login", { method: "DELETE" });
    state.sessionsRequestId += 1;
    state.authenticated = false;
    state.sessions = [];
    state.nextCursor = null;
    state.sessionsLoading = false;
    state.segmentsLoading = false;
    state.selectedSession = null;
    state.selectedDevices = null;
    state.connectionState = "idle";
    state.activeView = "transcript";
    clearTimeline();
    renderView();
    renderSessions();
    renderSessionDetails();
    renderConnectionState();
    hideStatus();
  }

  elements.loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = elements.tokenInput.value.trim();
    elements.tokenInput.value = "";
    setLoginError();
    try {
      await localFetch("/api/login", { method: "POST", body: JSON.stringify({ token }) });
      const bootstrap = await localFetch("/api/bootstrap");
      applyBootstrap(bootstrap);
      hideStatus();
    } catch (error) {
      setLoginError(error.message === "Authentication is required." ? "Token inválido." : error.message);
    }
  });
  elements.settingsButton.addEventListener("click", showSettings);
  elements.backToTranscriptButton.addEventListener("click", showTranscript);
  elements.logoutButton.addEventListener("click", () => signOut().catch((error) => showStatus(error.message, "error")));
  elements.newSessionButton.addEventListener("click", prepareNewSession);
  elements.loadMoreButton.addEventListener("click", () =>
    loadSessions().catch((error) => showStatus(error.message, "error")),
  );
  elements.refreshDevicesButton.addEventListener("click", showSettings);
  elements.settingsRefreshDevicesButton.addEventListener("click", refreshDevices);
  elements.settingsForm.addEventListener("submit", (event) => {
    saveSettings(event).catch((error) => showStatus(error.message, "error"));
  });
  elements.resetSettingsButton.addEventListener("click", () => {
    resetSettings().catch((error) => showStatus(error.message, "error"));
  });
  elements.settingsAudioTestButton.addEventListener("click", () => {
    if (state.audioTestActive) {
      stopAudioTest().catch(() => {});
    } else {
      startAudioTest();
    }
  });
  elements.proxyEnabled.addEventListener("change", () => {
    state.settings.proxyEnabled = elements.proxyEnabled.checked;
    updateProxyFieldsVisibility();
  });
  elements.themeLightOption.addEventListener("change", () => {
    if (elements.themeLightOption.checked) {
      state.settings.theme = "light";
      applyTheme("light");
    }
  });
  elements.themeDarkOption.addEventListener("change", () => {
    if (elements.themeDarkOption.checked) {
      state.settings.theme = "dark";
      applyTheme("dark");
    }
  });
  elements.settingsMicrophoneSelect.addEventListener("change", () => {
    state.settings.microphoneId = elements.settingsMicrophoneSelect.value;
    syncDeviceSelectTitle(elements.settingsMicrophoneSelect);
    if (state.audioTestActive) {
      stopAudioTest("Microfone alterado. Inicie o teste novamente para verificar o novo sinal.").catch(
        () => {},
      );
    }
  });
  elements.settingsSystemDeviceSelect.addEventListener("change", () => {
    state.settings.systemDeviceId = elements.settingsSystemDeviceSelect.value;
    syncDeviceSelectTitle(elements.settingsSystemDeviceSelect);
    if (state.audioTestActive) {
      stopAudioTest("Saída alterada. Inicie o teste novamente para verificar o novo sinal.").catch(
        () => {},
      );
    }
  });
  elements.captureToggleButton.addEventListener("click", () => {
    toggleCapture().catch((error) => showStatus(error.message, "error"));
  });
  elements.sessionTitle.addEventListener("change", () => {
    saveSessionTitle().catch((error) => {
      renderSessionDetails();
      showStatus(error.message, "error");
    });
  });
  elements.renameSessionCancel.addEventListener("click", closeRenameSession);
  elements.renameSessionConfirm.addEventListener("click", () => {
    confirmRenameSession().catch((error) => showStatus(error.message, "error"));
  });
  elements.renameSessionInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      confirmRenameSession().catch((error) => showStatus(error.message, "error"));
    }
  });
  elements.renameSessionModal.addEventListener("close", () => {
    state.renameSession = null;
  });
  elements.deleteSessionCancel.addEventListener("click", closeDeleteSession);
  elements.deleteSessionConfirm.addEventListener("click", () => {
    confirmDeleteSession().catch((error) => showStatus(error.message, "error"));
  });
  elements.deleteSessionModal.addEventListener("close", () => {
    state.deleteSession = null;
  });
  elements.copyCodeButton.addEventListener("click", copySessionCode);
  window.addEventListener("pagehide", () => {
    stopAudioTest().catch(() => {});
    closeAudioLevelStream();
  });
  window.addEventListener("pageshow", () => {
    if (state.authenticated) connectAudioLevels();
  });

  localFetch("/api/bootstrap")
    .then(applyBootstrap)
    .catch((error) => showStatus(error.message, "error"));
})();
