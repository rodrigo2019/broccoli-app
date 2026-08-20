(() => {
  "use strict";

  const elements = {
    loginView: document.querySelector("#loginView"),
    mainView: document.querySelector("#mainView"),
    loginForm: document.querySelector("#loginForm"),
    tokenInput: document.querySelector("#tokenInput"),
    loginError: document.querySelector("#loginError"),
    sidebarFooter: document.querySelector("#sidebarFooter"),
    logoutButton: document.querySelector("#logoutButton"),
    statusBanner: document.querySelector("#statusBanner"),
    statusMessage: document.querySelector("#statusMessage"),
    newSessionButton: document.querySelector("#newSessionButton"),
    loadMoreButton: document.querySelector("#loadMoreButton"),
    sessionLibrary: document.querySelector("#sessionLibrary"),
    sessionTitle: document.querySelector("#sessionTitle"),
    sessionMeta: document.querySelector("#sessionMeta"),
    microphoneSelect: document.querySelector("#microphoneSelect"),
    systemDeviceSelect: document.querySelector("#systemDeviceSelect"),
    refreshDevicesButton: document.querySelector("#refreshDevicesButton"),
    deviceRequired: document.querySelector("#deviceRequired"),
    startSessionButton: document.querySelector("#startSessionButton"),
    resumeSessionButton: document.querySelector("#resumeSessionButton"),
    stopSessionButton: document.querySelector("#stopSessionButton"),
    copyCodeButton: document.querySelector("#copyCodeButton"),
    openBroccoliLink: document.querySelector("#openBroccoliLink"),
    recordingNotice: document.querySelector("#recordingNotice"),
    recordingIdleNotice: document.querySelector("#recordingIdleNotice"),
    recordingTime: document.querySelector("#recordingTime"),
    connectionBadge: document.querySelector("#connectionBadge"),
    captureDock: document.querySelector("#captureDock"),
    waveformBars: document.querySelector("#waveformBars"),
    microphoneMeter: document.querySelector("#microphoneMeter"),
    systemMeter: document.querySelector("#systemMeter"),
    transcriptTimeline: document.querySelector("#transcriptTimeline"),
    emptyTimeline: document.querySelector("#emptyTimeline"),
  };

  const state = {
    authenticated: false,
    capabilities: {
      history: false,
      remote_title: false,
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
    captureStartedAt: null,
    captureTimer: null,
  };

  const badgeClasses = {
    idle: "badge badge-neutral hidden sm:inline-flex",
    starting: "badge badge-info hidden sm:inline-flex",
    streaming: "badge badge-success hidden sm:inline-flex",
    reconnecting: "badge badge-warning hidden sm:inline-flex",
    stopped: "badge badge-neutral hidden sm:inline-flex",
    failed: "badge badge-error hidden sm:inline-flex",
    device_selection_required: "badge badge-error hidden sm:inline-flex",
  };

  class CaptureMotion {
    constructor({ waveformBars, microphoneMeter, systemMeter }) {
      this.waveformBars = waveformBars;
      this.microphoneMeter = microphoneMeter;
      this.systemMeter = systemMeter;
      this.waveBars = [];
      this.meterBars = [];
      this.captureState = "idle";
      this.animationFrame = null;
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }

    mount() {
      this.waveBars = this.createBars(this.waveformBars, 48, "wave-bar");
      this.meterBars = [
        ...this.createBars(this.microphoneMeter, 8, "meter-bar"),
        ...this.createBars(this.systemMeter, 8, "meter-bar"),
      ];
      this.paint(0);
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
      if (this.isActive() && !this.reducedMotion) {
        this.start();
      } else {
        this.stop();
        this.paint(0);
      }
    }

    isActive() {
      return ["starting", "streaming", "reconnecting"].includes(this.captureState);
    }

    start() {
      if (this.animationFrame !== null) return;
      this.animationFrame = window.requestAnimationFrame((time) => this.tick(time));
    }

    stop() {
      if (this.animationFrame === null) return;
      window.cancelAnimationFrame(this.animationFrame);
      this.animationFrame = null;
    }

    tick(time) {
      this.animationFrame = null;
      if (!this.isActive()) return;
      this.paint(time);
      this.start();
    }

    paint(time) {
      const active = this.isActive();
      const intensity = active && this.captureState === "streaming" ? 1 : 0.5;
      this.waveBars.forEach((bar, index) => {
        const center = 1 - Math.abs(index / Math.max(this.waveBars.length - 1, 1) - 0.5) * 1.55;
        const movement = 0.5 + 0.5 * Math.sin(time * 0.008 + index * 1.63);
        const secondary = 0.5 + 0.5 * Math.sin(time * 0.0037 + index * 0.73 + 1.2);
        const height = active ? 12 + (movement * 0.58 + secondary * 0.42) * 64 * center * intensity : 16 + (index % 3) * 4;
        bar.style.setProperty("--wave-height", `${height.toFixed(1)}%`);
      });
      this.meterBars.forEach((bar, index) => {
        const movement = 0.45 + 0.55 * Math.sin(time * 0.01 + index * 1.8);
        bar.style.opacity = active ? `${0.55 + movement * 0.45 * intensity}` : "0.45";
      });
    }
  }

  const captureMotion = new CaptureMotion(elements);
  captureMotion.mount();

  async function localFetch(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (response.ok) {
      return response.status === 204 ? null : response.json();
    }
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
    elements.loginView.classList.toggle("hidden", state.authenticated);
    elements.mainView.classList.toggle("hidden", !state.authenticated);
    elements.sidebarFooter.classList.toggle("hidden", !state.authenticated);
    elements.newSessionButton.classList.toggle("hidden", !state.authenticated);
  }

  function renderDevices(devices) {
    const selected = state.selectedDevices || {};
    renderDeviceSelect(elements.microphoneSelect, devices.filter((device) => device.kind === "mic"), selected.microphone_id);
    renderDeviceSelect(
      elements.systemDeviceSelect,
      devices.filter((device) => device.kind === "system"),
      selected.system_device_id,
    );
    updateDeviceRequirement();
  }

  function updateDeviceRequirement() {
    const required = !elements.microphoneSelect.value || !elements.systemDeviceSelect.value;
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
    for (const session of state.sessions) {
      const item = document.createElement("li");
      item.className = "min-w-0 max-w-full shrink-0 overflow-hidden";
      const row = document.createElement("button");
      row.type = "button";
      const active = state.selectedSession?.uuid_code === session.uuid_code;
      row.className = active
        ? "btn h-auto min-h-0 w-full min-w-0 max-w-full flex-col items-start justify-start gap-1 overflow-hidden whitespace-normal border border-primary/20 bg-primary/15 px-3 py-3 text-left normal-case text-base-content hover:bg-primary/20"
        : "btn btn-ghost h-auto min-h-0 w-full min-w-0 max-w-full flex-col items-start justify-start gap-1 overflow-hidden whitespace-normal px-3 py-3 text-left normal-case";
      row.dataset.testid = `session-row-${session.uuid_code}`;
      const sessionLabel = session.title || session.device_label || session.uuid_code;
      row.setAttribute("aria-label", `Abrir sessão ${sessionLabel}`);
      row.title = sessionLabel;
      const title = document.createElement("span");
      title.className = "block w-full min-w-0 max-w-full truncate text-left font-semibold";
      title.title = sessionLabel;
      title.textContent = sessionLabel;
      const details = document.createElement("span");
      details.className = active
        ? "block max-w-full truncate text-xs text-primary"
        : "block max-w-full truncate text-xs text-base-content/60";
      details.textContent = `${session.segment_count} segmentos`;
      row.append(title, details);
      row.addEventListener("click", () => {
        selectSession(session).catch((error) => showStatus(error.message, "error"));
      });
      item.append(row);
      elements.sessionLibrary.append(item);
    }
  }

  function mergeSession(session) {
    const index = state.sessions.findIndex((item) => item.uuid_code === session.uuid_code);
    if (index === -1) {
      state.sessions.unshift(session);
    } else {
      const existing = state.sessions[index];
      state.sessions[index] = {
        ...existing,
        ...session,
        // Titles are local until the backend stores them. Do not let a remote
        // history refresh erase the title already shown for the live row.
        title: session.title || existing.title,
      };
    }
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

  function formatCaptureTime(milliseconds) {
    const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function updateCaptureTimer() {
    if (isCaptureActive() && !state.captureStartedAt) {
      state.captureStartedAt = Date.now();
    }
    if (state.captureStartedAt) {
      elements.recordingTime.textContent = formatCaptureTime(Date.now() - state.captureStartedAt);
    } else {
      elements.recordingTime.textContent = "00:00";
    }
    if (isCaptureActive() && state.captureTimer === null) {
      state.captureTimer = window.setInterval(updateCaptureTimer, 1000);
    } else if (!isCaptureActive() && state.captureTimer !== null) {
      window.clearInterval(state.captureTimer);
      state.captureTimer = null;
    }
  }

  function renderCaptureDock() {
    const active = isCaptureActive();
    elements.captureDock.dataset.captureState = state.connectionState;
    elements.recordingNotice.classList.toggle("hidden", !active);
    elements.recordingIdleNotice.classList.toggle("hidden", active);
    captureMotion.setState(state.connectionState);
    updateCaptureTimer();
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

  function renderSessionDetails() {
    const session = state.selectedSession;
    elements.sessionTitle.value = session?.title || "";
    elements.sessionMeta.textContent = session
      ? `Código ${session.uuid_code} · ${session.segment_count} segmentos · título somente local`
      : "Inicie uma captura para gerar um código local.";
    elements.sessionTitle.title = state.capabilities.remote_title
      ? ""
      : "Este título é mantido somente durante esta captura.";
    elements.copyCodeButton.disabled = !session;
    const active = ["starting", "streaming", "reconnecting"].includes(state.connectionState);
    elements.resumeSessionButton.disabled = !state.capabilities.user_resume || !session || active;
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
    elements.connectionBadge.className = badgeClasses[state.connectionState] || "badge badge-neutral";
    elements.connectionBadge.textContent = label;
    const active = state.connectionState === "starting" || state.connectionState === "streaming" || state.connectionState === "reconnecting";
    elements.stopSessionButton.disabled = !active;
    elements.startSessionButton.disabled = active;
    elements.startSessionButton.textContent = state.selectedSession
      ? "Continuar captura"
      : "Iniciar captura";
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
    try {
      const response = await localFetch("/api/devices");
      renderDevices(response.devices);
    } catch (error) {
      showStatus(error.message, "error");
    }
  }

  function selectedDevicePayload() {
    return {
      microphone_id: elements.microphoneSelect.value,
      system_device_id: elements.systemDeviceSelect.value,
    };
  }

  async function startSession() {
    const devices = selectedDevicePayload();
    if (!devices.microphone_id || !devices.system_device_id) {
      elements.deviceRequired.classList.remove("hidden");
      return;
    }
    const previousConnectionState = state.connectionState;
    const previousCaptureStartedAt = state.captureStartedAt;
    const currentSession = state.selectedSession;
    const path = currentSession
      ? `/api/sessions/${encodeURIComponent(currentSession.uuid_code)}/resume`
      : "/api/sessions";
    const body = currentSession
      ? devices
      : { ...devices, title: elements.sessionTitle.value };
    try {
      state.connectionState = "starting";
      state.captureStartedAt = Date.now();
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
      state.captureStartedAt = previousCaptureStartedAt;
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

  function saveLocalTitle() {
    const session = state.selectedSession;
    if (!session) return;
    state.selectedSession = { ...session, title: elements.sessionTitle.value.trim() };
    renderSessionDetails();
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
    state.selectedSession = null;
    state.connectionState = "idle";
    state.captureStartedAt = null;
    clearTimeline();
    renderSessions();
    renderSessionDetails();
    renderConnectionState();
    elements.sessionTitle.focus();
  }

  function applyBootstrap(bootstrap, connectToEvents = true) {
    state.authenticated = bootstrap.authenticated;
    state.capabilities = bootstrap.capabilities;
    state.selectedDevices = bootstrap.selected_devices;
    state.connectionState = bootstrap.state;
    state.selectedSession = bootstrap.session;
    state.captureStartedAt = isCaptureActive() ? state.captureStartedAt || Date.now() : null;
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
    state.captureStartedAt = null;
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
  elements.logoutButton.addEventListener("click", () => signOut().catch((error) => showStatus(error.message, "error")));
  elements.newSessionButton.addEventListener("click", prepareNewSession);
  elements.loadMoreButton.addEventListener("click", () =>
    loadSessions().catch((error) => showStatus(error.message, "error")),
  );
  elements.refreshDevicesButton.addEventListener("click", refreshDevices);
  elements.microphoneSelect.addEventListener("change", () => {
    syncDeviceSelectTitle(elements.microphoneSelect);
    updateDeviceRequirement();
  });
  elements.systemDeviceSelect.addEventListener("change", () => {
    syncDeviceSelectTitle(elements.systemDeviceSelect);
    updateDeviceRequirement();
  });
  elements.startSessionButton.addEventListener("click", startSession);
  elements.resumeSessionButton.addEventListener("click", startSession);
  elements.stopSessionButton.addEventListener("click", stopSession);
  elements.sessionTitle.addEventListener("change", saveLocalTitle);
  elements.copyCodeButton.addEventListener("click", copySessionCode);

  localFetch("/api/bootstrap")
    .then(applyBootstrap)
    .catch((error) => showStatus(error.message, "error"));
})();
