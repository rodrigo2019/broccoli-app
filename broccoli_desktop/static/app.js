(() => {
  "use strict";

  const elements = {
    loginView: document.querySelector("#loginView"),
    mainView: document.querySelector("#mainView"),
    loginForm: document.querySelector("#loginForm"),
    tokenInput: document.querySelector("#tokenInput"),
    loginError: document.querySelector("#loginError"),
    logoutButton: document.querySelector("#logoutButton"),
    statusBanner: document.querySelector("#statusBanner"),
    statusMessage: document.querySelector("#statusMessage"),
    sessionSearch: document.querySelector("#sessionSearch"),
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
    connectionBadge: document.querySelector("#connectionBadge"),
    transcriptTimeline: document.querySelector("#transcriptTimeline"),
    emptyTimeline: document.querySelector("#emptyTimeline"),
  };

  const state = {
    authenticated: false,
    capabilities: {
      history: false,
      remote_title: false,
      user_resume: false,
      segment_history: false,
    },
    sessions: [],
    nextCursor: null,
    selectedSession: null,
    selectedDevices: null,
    connectionState: "idle",
    pendingDeltas: new Map(),
    eventSocket: null,
  };

  const badgeClasses = {
    idle: "badge badge-neutral",
    starting: "badge badge-info",
    streaming: "badge badge-success",
    reconnecting: "badge badge-warning",
    stopped: "badge badge-neutral",
    failed: "badge badge-error",
    device_selection_required: "badge badge-error",
  };

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
    elements.logoutButton.classList.toggle("hidden", !state.authenticated);
  }

  function renderDevices(devices) {
    const selected = state.selectedDevices || {};
    renderDeviceSelect(elements.microphoneSelect, devices.filter((device) => device.kind === "mic"), selected.microphone_id);
    renderDeviceSelect(
      elements.systemDeviceSelect,
      devices.filter((device) => device.kind === "system"),
      selected.system_device_id,
    );
    const required = !elements.microphoneSelect.value || !elements.systemDeviceSelect.value;
    elements.deviceRequired.classList.toggle("hidden", !required);
  }

  function renderDeviceSelect(select, devices, selectedDeviceId) {
    select.replaceChildren();
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Selecionar dispositivo";
    select.append(empty);
    for (const device of devices) {
      const option = document.createElement("option");
      option.value = device.device_id;
      option.textContent = device.label;
      option.selected = device.device_id === selectedDeviceId;
      select.append(option);
    }
  }

  function renderSessions() {
    elements.sessionLibrary.replaceChildren();
    elements.sessionSearch.disabled = !state.capabilities.history;
    elements.loadMoreButton.disabled = !state.capabilities.history || !state.nextCursor;
    if (!state.capabilities.history) {
      const unavailable = document.createElement("p");
      unavailable.className = "empty-history";
      unavailable.textContent = "Histórico não disponível neste backend.";
      elements.sessionLibrary.append(unavailable);
      return;
    }
    if (!state.sessions.length) {
      const empty = document.createElement("p");
      empty.className = "empty-history";
      empty.textContent = "Nenhuma sessão encontrada.";
      elements.sessionLibrary.append(empty);
    }
    for (const session of state.sessions) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "session-row text-left";
      row.dataset.testid = `session-row-${session.uuid_code}`;
      row.setAttribute("aria-label", `Abrir sessão ${session.title || session.uuid_code}`);
      row.classList.toggle("session-row-active", state.selectedSession?.uuid_code === session.uuid_code);
      const title = document.createElement("strong");
      title.textContent = session.title || "Sem título";
      const details = document.createElement("span");
      details.className = "text-xs text-base-content/60";
      details.textContent = `${session.segment_count} segmentos`;
      row.append(title, details);
      row.addEventListener("click", () => {
        state.selectedSession = session;
        clearTimeline();
        renderSessions();
        renderSessionDetails();
      });
      elements.sessionLibrary.append(row);
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
    elements.resumeSessionButton.disabled = !state.capabilities.user_resume || !session;
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
    elements.recordingNotice.classList.toggle("hidden", !active);
    elements.stopSessionButton.disabled = !active;
    elements.startSessionButton.disabled = active;
    if (state.connectionState === "reconnecting") {
      showStatus("Reconectando à transcrição…", "warning");
    }
    if (state.connectionState === "device_selection_required") {
      elements.deviceRequired.classList.remove("hidden");
      showStatus("Selecione os dispositivos antes de continuar.", "error");
    }
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
    row.className = isDelta ? "transcript-row transcript-row-delta" : "transcript-row";
    row.dataset.utteranceId = entry.utterance_id;
    const timestamp = document.createElement("time");
    timestamp.className = "transcript-timestamp text-xs text-base-content/60";
    timestamp.textContent = formatTranscriptTimestamp(entry.started_offset_ms);
    const channel = document.createElement("span");
    channel.className = entry.channel === "mic" ? "channel-label channel-label-mic" : "channel-label channel-label-system";
    channel.textContent = entry.channel === "mic" ? "Você" : "Participantes";
    const text = document.createElement("p");
    text.className = isDelta ? "italic text-base-content/70" : "";
    text.textContent = entry.text;
    row.append(timestamp, channel, text);
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
    empty.className = "empty-history";
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

  async function startNewSession() {
    const devices = selectedDevicePayload();
    if (!devices.microphone_id || !devices.system_device_id) {
      elements.deviceRequired.classList.remove("hidden");
      return;
    }
    try {
      state.connectionState = "starting";
      clearTimeline();
      renderConnectionState();
      const session = await localFetch("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ ...devices, title: elements.sessionTitle.value }),
      });
      state.selectedSession = session;
      state.sessions = [];
      renderSessions();
      renderSessionDetails();
    } catch (error) {
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
    state.capabilities = bootstrap.capabilities;
    state.selectedDevices = bootstrap.selected_devices;
    state.connectionState = bootstrap.state;
    state.selectedSession = bootstrap.session;
    state.sessions = bootstrap.sessions.sessions;
    state.nextCursor = bootstrap.sessions.next_cursor;
    elements.openBroccoliLink.href = bootstrap.official_broccoli_url;
    renderView();
    renderSessions();
    renderSessionDetails();
    renderConnectionState();
    clearTimeline();
    if (state.authenticated) {
      refreshDevices();
      if (connectToEvents) connectEvents();
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
      if (event.session) state.selectedSession = event.session;
      renderSessions();
      renderSessionDetails();
      renderConnectionState();
    } else if (event.type === "session" && event.session) {
      state.selectedSession = event.session;
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
        window.setTimeout(connectEvents, 1000);
      }
    });
  }

  async function signOut() {
    state.eventSocket?.close();
    state.eventSocket = null;
    await localFetch("/api/login", { method: "DELETE" });
    state.authenticated = false;
    state.sessions = [];
    state.selectedSession = null;
    clearTimeline();
    renderView();
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
  elements.refreshDevicesButton.addEventListener("click", refreshDevices);
  elements.startSessionButton.addEventListener("click", startNewSession);
  elements.stopSessionButton.addEventListener("click", stopSession);
  elements.sessionTitle.addEventListener("change", saveLocalTitle);
  elements.copyCodeButton.addEventListener("click", copySessionCode);

  localFetch("/api/bootstrap")
    .then(applyBootstrap)
    .catch((error) => showStatus(error.message, "error"));
})();
