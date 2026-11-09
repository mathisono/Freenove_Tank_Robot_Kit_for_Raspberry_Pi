/* Rover Field Control. No external scripts, credentials, or queued drive commands. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const video = $("roverVideo");
  const overlay = $("targetOverlay");
  const padButtons = [...document.querySelectorAll("[data-drive]")];
  const keyDirections = {KeyW: "forward", ArrowUp: "forward", KeyS: "backward", ArrowDown: "backward", KeyA: "left", ArrowLeft: "left", KeyD: "right", ArrowRight: "right"};
  const keys = new Set();
  const pointers = new Map();
  const phases = {
    idle: ["Standing by", "Grab begins a ball pickup. Release opens the claw."],
    acquiring: ["Finding the ball", "Looking for a clear ball target in the camera view."],
    aligning: ["Aligning", "Turning to center the ball in front of the claw."],
    approaching: ["Approaching", "Moving slowly while checking the ball and front distance."],
    gripping: ["Closing the claw", "The rover has stopped to close the claw."],
    lifting: ["Lifting the claw", "Holding the close command while raising the arm."],
    holding: ["Grip held", "Close command held until Release. Inspect the ball in the live view."],
    releasing: ["Releasing", "Stopping movement and opening the claw."],
    stopped: ["Movement stopped", "Enable controls when you're ready to continue."],
    error: ["Retrieval stopped", "Check the rover before starting another pickup."],
  };
  const autonomousPhases = new Set(["acquiring", "aligning", "approaching", "gripping", "lifting"]);
  let state = {};
  let socket = null;
  let peer = null;
  let ticket = null;
  let seq = 0;
  let reconnectTimer = null;
  let videoReconnectTimer = null;
  let localEnabled = false;
  let enableRequested = false;
  let moving = false;
  let authenticated = false;
  let videoConnecting = false;
  let lastRenderedFrame = 0;
  let lastStats = null;
  let frameCallbackStarted = false;
  let wasVideoHealthy = false;
  let videoGeneration = 0;
  let bootstrapGeneration = 0;
  let noticeTimer = null;
  let retired = false;

  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const clamp = (number, low, high) => Math.max(low, Math.min(high, number));
  const isSocketOpen = () => socket && socket.readyState === WebSocket.OPEN;
  const ownsControl = () => state.control_owned === true;
  const controlBusy = () => state.control_busy === true || (!!state.owner && !ownsControl());
  const currentPhase = () => state.phase || state.status || "idle";

  function announce(text) {
    $("announcer").textContent = text;
  }

  function showNotice(text, error = false, persistent = false) {
    clearTimeout(noticeTimer);
    const node = $("notice");
    node.textContent = text;
    node.classList.toggle("error", error);
    node.hidden = false;
    if (!persistent) noticeTimer = setTimeout(() => { node.hidden = true; }, 6500);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {credentials: "same-origin", cache: "no-store", ...options});
    let body;
    try { body = await response.json(); } catch (_) { body = {}; }
    if (!response.ok) {
      const error = new Error(body.message || body.error || `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function send(payload, {urgent = false} = {}) {
    if (!isSocketOpen()) return false;
    const exempt = payload.type === "ping" || (payload.type === "action" && payload.action === "stop");
    if (!exempt && !ticket) return false;
    // Never build up motion behind a congested connection. The rover's command
    // expiry and deadman timer also enforce this independently of the browser.
    if (!urgent && socket.bufferedAmount > 4096) return false;
    try {
      socket.send(JSON.stringify({...payload, ...(exempt ? {} : {ticket})}));
      return true;
    } catch (_) { return false; }
  }

  function freshVideo() {
    const camera = state.camera || {};
    const recentFrame = lastRenderedFrame > 0 && performance.now() - lastRenderedFrame < 900;
    const freshCamera = camera.available !== false && finite(camera.frame_age) && camera.frame_age <= 0.75;
    return !!(recentFrame && freshCamera && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && peer && peer.connectionState === "connected" && !document.hidden);
  }

  function activeDirections() {
    return new Set([...keys].map((key) => keyDirections[key]).concat([...pointers.values()]));
  }

  function driveVector() {
    const directions = activeDirections();
    const speed = Number($("speedLimit").value) / 100;
    return {
      throttle: ((directions.has("forward") ? 1 : 0) - (directions.has("backward") ? 1 : 0)) * speed,
      steering: ((directions.has("right") ? 1 : 0) - (directions.has("left") ? 1 : 0)) * speed,
    };
  }

  function paintPad() {
    const directions = activeDirections();
    for (const button of padButtons) button.classList.toggle("pressed", directions.has(button.dataset.drive));
  }

  function zeroDrive() {
    if (moving) send({type: "drive", throttle: 0, steering: 0, seq: ++seq}, {urgent: true});
    moving = false;
  }

  function clearInput() {
    keys.clear();
    pointers.clear();
    paintPad();
    zeroDrive();
  }

  function driveTick() {
    if (!localEnabled || !ownsControl() || !freshVideo()) {
      zeroDrive();
      return;
    }
    const vector = driveVector();
    const nonzero = vector.throttle !== 0 || vector.steering !== 0;
    if (nonzero) {
      if (send({type: "drive", ...vector, seq: ++seq})) moving = true;
    } else {
      // An idle joystick must not continually cancel an autonomous Grab action.
      zeroDrive();
    }
    paintPad();
  }

  function stopRover(reason = "Movement stopped.", quiet = false) {
    clearInput();
    send({type: "action", action: "stop"}, {urgent: true});
    localEnabled = false;
    enableRequested = false;
    updateControls();
    if (!quiet) showNotice(reason);
    announce("Rover stopped");
  }

  function pauseForFocusLoss() {
    if (localEnabled || enableRequested || moving || (ownsControl() && autonomousPhases.has(currentPhase()))) {
      stopRover("Controls paused. Enable controls again to continue.", true);
    } else {
      clearInput();
    }
  }

  function updateControls() {
    const linked = isSocketOpen();
    const healthy = freshVideo();
    const owned = ownsControl();
    const enabled = localEnabled && owned && state.enabled === true && healthy && linked;
    for (const button of padButtons) button.disabled = !enabled;
    const enable = $("enableButton");
    enable.disabled = !linked || !healthy || !ticket || (controlBusy() && !owned) || enableRequested;
    enable.classList.toggle("active", enabled);
    $("enableLabel").textContent = enableRequested ? "Enabling…" : enabled ? "Disable controls" : "Enable controls";
    enable.title = !linked ? "Connect to the rover first" : !healthy ? "A fresh live camera view is required" : controlBusy() && !owned ? "Another operator has control" : "Take control of the rover";
    $("controlState").textContent = enabled ? "ENABLED" : enableRequested ? "WAITING" : "DISABLED";
    $("controlState").classList.toggle("enabled", enabled);
    const calibrated = state.autonomy_calibrated === true;
    $("grabButton").disabled = !enabled || !calibrated || autonomousPhases.has(currentPhase()) || state.grip_closed === true;
    $("grabButton").title = !calibrated ? "Ball pickup needs calibration on the rover" : !enabled ? "Enable controls first" : "Approach the visible ball and close the claw";
    // Release is a non-driving action. The server permits the owner, or an
    // observer when there is no owner, to stop and open the claw without video.
    $("releaseButton").disabled = !linked || !ticket || (controlBusy() && !owned) || currentPhase() === "releasing";
    $("stopButton").disabled = !authenticated || !linked;
    $("footerStatus").textContent = enabled ? "You have control" : controlBusy() && !owned ? "Viewing · another operator has control" : "Controls disabled";
  }

  function setConnection(connected, label) {
    $("connectionPill").classList.toggle("connected", connected);
    $("connectionLabel").textContent = label;
  }

  function renderState(next) {
    const oldPhase = currentPhase();
    state = next || {};
    if (enableRequested && state.enabled === true && ownsControl()) {
      enableRequested = false;
      localEnabled = true;
      announce("Rover controls enabled");
    }
    if (!ownsControl() || state.enabled !== true) {
      localEnabled = false;
      clearInput();
    }
    $("demoBadge").hidden = state.demo !== true;
    const distance = state.distance_cm;
    $("distanceValue").textContent = finite(distance) && distance >= 0 ? Math.round(distance).toString() : "—";
    $("distanceNote").textContent = finite(distance) && distance >= 0 ? "Ultrasonic sensor" : "No valid range reading";
    const target = state.target || {};
    $("targetLabel").textContent = target.valid ? "BALL DETECTED" : "NO TARGET LOCK";
    $("visionStatus").textContent = target.valid ? "Ball in view" : "Vision standing by";

    const phase = currentPhase();
    const active = autonomousPhases.has(phase);
    const closed = state.grip_closed === true;
    const details = phases[phase] || [phase.replaceAll("_", " "), state.reason || ""];
    $("retrievalLabel").textContent = phase === "idle" && closed ? "Grip held" : details[0];
    let detail = state.reason || details[1];
    // An explicit stop/fault reason takes priority over generic setup advice.
    if (!state.reason) {
      if (phase === "idle" && !closed && state.autonomy_calibrated === false) detail = "Ball pickup needs calibration. Direct drive is available.";
      else if (phase === "idle" && closed) detail = phases.holding[1];
      else if (phase === "idle" && !localEnabled) detail = "Enable controls to begin a ball pickup.";
    }
    $("retrievalDetail").textContent = detail;
    $("retrievalDot").className = `status-dot ${active ? "warning" : closed ? "good" : phase === "error" ? "bad" : ""}`;
    $("grabButton").classList.toggle("active", active || closed);
    $("grabButton").setAttribute("aria-pressed", String(active || closed));
    const released = state.grip_closed === false && !active;
    $("releaseButton").classList.toggle("active", released);
    $("releaseButton").setAttribute("aria-pressed", String(released));
    const step = active || closed ? ["acquiring", "aligning"].includes(phase) ? 0 : phase === "approaching" ? 1 : 2 : -1;
    document.querySelectorAll("[data-step]").forEach((element, index) => {
      element.classList.toggle("active", index === step && !closed);
      element.classList.toggle("complete", index < step || closed);
    });
    if (phase !== oldPhase) announce($("retrievalLabel").textContent);
    updateControls();
    drawTarget();
  }

  function handleSocketMessage(event) {
    let payload;
    try { payload = JSON.parse(event.data); } catch (_) { return; }
    if (payload.type === "state") {
      if (typeof payload.ticket === "string") ticket = payload.ticket;
      renderState(payload.state);
    } else if (payload.type === "pong" && finite(payload.sent_at)) {
      const elapsed = performance.now() - payload.sent_at;
      if (elapsed >= 0 && elapsed < 10000) $("latencyValue").textContent = Math.round(elapsed).toString();
    } else if (payload.type === "error") {
      const message = String(payload.message || payload.error || "The rover could not complete that command.");
      const stale = payload.code === "stale_ticket" || /stale[_ ]ticket|expired.*ticket|ticket.*expired/i.test(message);
      if (enableRequested) enableRequested = false;
      if (!stale) showNotice(message, true);
      updateControls();
    }
  }

  function connectSocket() {
    if (!authenticated || retired || document.hidden) return;
    clearTimeout(reconnectTimer);
    if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) return;
    ticket = null;
    localEnabled = false;
    enableRequested = false;
    setConnection(false, "Connecting");
    const connection = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws`);
    socket = connection;
    connection.addEventListener("open", () => {
      if (socket !== connection) return;
      setConnection(true, "Rover connected");
      updateControls();
    });
    connection.addEventListener("message", handleSocketMessage);
    connection.addEventListener("close", () => {
      if (socket !== connection) return;
      clearInput();
      localEnabled = false;
      enableRequested = false;
      ticket = null;
      state = {...state, enabled: false, control_owned: false};
      $("latencyValue").textContent = "—";
      setConnection(false, "Link disconnected");
      updateControls();
      if (!retired && !document.hidden) reconnectTimer = setTimeout(connectSocket, 2000);
    });
    connection.addEventListener("error", () => setConnection(false, "Link unavailable"));
  }

  function waitForIce(connection) {
    if (connection.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error("Video connection setup timed out. Try reconnecting.")), 5000);
      const check = () => {
        if (connection.iceGatheringState === "complete") finish();
        else if (connection.connectionState === "closed") finish(new Error("Video connection was closed."));
      };
      function finish(error) {
        clearTimeout(timer);
        connection.removeEventListener("icegatheringstatechange", check);
        error ? reject(error) : resolve();
      }
      connection.addEventListener("icegatheringstatechange", check);
    });
  }

  function closeVideo() {
    ++videoGeneration;
    clearTimeout(videoReconnectTimer);
    const old = peer;
    peer = null;
    videoConnecting = false;
    lastRenderedFrame = 0;
    lastStats = null;
    if (old) old.close();
    if (video.srcObject) video.srcObject.getTracks().forEach((track) => track.stop());
    video.srcObject = null;
    $("fpsValue").textContent = "—";
    renderVideoHealth();
  }

  function videoFailure(message) {
    if (localEnabled || moving || (ownsControl() && autonomousPhases.has(currentPhase()))) stopRover("Video interrupted — movement stopped.");
    $("videoEmptyTitle").textContent = "Camera link unavailable";
    $("videoEmptyText").textContent = message || "Check the camera, then reconnect.";
    if (!retired && !document.hidden && authenticated) {
      clearTimeout(videoReconnectTimer);
      videoReconnectTimer = setTimeout(() => { closeVideo(); connectVideo(); }, 3000);
    }
  }

  async function connectVideo() {
    if (!authenticated || videoConnecting || peer || document.hidden || retired) return;
    if (!window.RTCPeerConnection) {
      videoFailure("This browser does not support the rover's live video connection.");
      return;
    }
    videoConnecting = true;
    const generation = ++videoGeneration;
    $("videoEmptyTitle").textContent = "Connecting to the camera";
    $("videoEmptyText").textContent = "Establishing a direct live video link.";
    const connection = new RTCPeerConnection({iceServers: []});
    peer = connection;
    const transceiver = connection.addTransceiver("video", {direction: "recvonly"});
    if (window.RTCRtpReceiver && transceiver.setCodecPreferences) {
      const capabilities = RTCRtpReceiver.getCapabilities("video");
      if (capabilities) {
        const h264 = capabilities.codecs.filter((codec) => codec.mimeType.toLowerCase() === "video/h264");
        const rest = capabilities.codecs.filter((codec) => codec.mimeType.toLowerCase() !== "video/h264");
        if (h264.length) {
          try { transceiver.setCodecPreferences([...h264, ...rest]); } catch (_) { /* Browser default is valid for demo video. */ }
        }
      }
    }
    connection.addEventListener("track", (event) => {
      if (peer !== connection || generation !== videoGeneration) return;
      // Ask supporting browsers for minimal playout buffering. This is only
      // a hint, not a guarantee or measurement of camera-to-display latency.
      if (event.receiver && "jitterBufferTarget" in event.receiver) {
        try { event.receiver.jitterBufferTarget = 0; } catch (_) { /* Optional browser capability. */ }
      }
      video.srcObject = event.streams[0] || new MediaStream([event.track]);
      video.play().catch(() => {
        $("videoEmptyText").textContent = "Tap Reconnect to start live video.";
      });
      event.track.addEventListener("ended", () => { if (peer === connection) videoFailure("The camera stream ended."); });
    });
    connection.addEventListener("connectionstatechange", () => {
      if (peer !== connection) return;
      if (["failed", "disconnected"].includes(connection.connectionState)) videoFailure("The video link was interrupted. Reconnecting…");
      renderVideoHealth();
    });
    try {
      await connection.setLocalDescription(await connection.createOffer());
      await waitForIce(connection);
      if (generation !== videoGeneration) return;
      const answer = await api("/api/offer", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({sdp: connection.localDescription.sdp, type: connection.localDescription.type})});
      if (generation !== videoGeneration) return;
      await connection.setRemoteDescription(answer);
    } catch (error) {
      if (generation !== videoGeneration) return;
      videoFailure(error.message);
    } finally {
      if (generation === videoGeneration) videoConnecting = false;
    }
  }

  function watchRenderedFrames() {
    if (frameCallbackStarted || !video.requestVideoFrameCallback) return;
    frameCallbackStarted = true;
    const callback = (now) => {
      if (peer && !document.hidden && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) lastRenderedFrame = now;
      drawTarget();
      if (!retired) video.requestVideoFrameCallback(callback);
    };
    video.requestVideoFrameCallback(callback);
  }

  async function pollVideoStats() {
    if (!peer || peer.connectionState !== "connected") return;
    const connection = peer;
    try {
      const reports = await connection.getStats();
      if (peer !== connection) return;
      reports.forEach((report) => {
        if (report.type !== "inbound-rtp" || ![report.kind, report.mediaType].includes("video") || !finite(report.framesDecoded)) return;
        if (lastStats && report.timestamp > lastStats.timestamp) {
          const delta = report.framesDecoded - lastStats.frames;
          const rate = delta * 1000 / (report.timestamp - lastStats.timestamp);
          $("fpsValue").textContent = Math.max(0, Math.round(rate)).toString();
          if (!video.requestVideoFrameCallback && delta > 0 && !document.hidden) lastRenderedFrame = performance.now();
        }
        lastStats = {frames: report.framesDecoded, timestamp: report.timestamp};
      });
    } catch (_) { /* A closing connection has no further stats. */ }
  }

  function renderVideoHealth() {
    const healthy = freshVideo();
    if (wasVideoHealthy && !healthy && (localEnabled || moving || (ownsControl() && autonomousPhases.has(currentPhase())))) {
      stopRover("Live video is stale — movement stopped. Re-enable after the picture returns.");
    }
    wasVideoHealthy = healthy;
    $("liveBadge").classList.toggle("live", healthy);
    $("liveLabel").textContent = healthy ? "LIVE" : videoConnecting ? "CONNECTING" : "CAMERA OFFLINE";
    const hasPicture = video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && lastRenderedFrame > 0;
    $("videoEmpty").hidden = healthy;
    $("videoGrid").hidden = hasPicture;
    video.classList.toggle("stale", !healthy && hasPicture);
    if (hasPicture && !healthy && !videoConnecting) {
      $("videoEmptyTitle").textContent = "Waiting for fresh video";
      $("videoEmptyText").textContent = "Drive controls pause when the live picture is interrupted.";
    }
    $("cameraStatusDot").className = `status-dot ${healthy ? "good" : ""}`;
    const camera = state.camera || {};
    $("cameraStatusText").textContent = healthy ? state.demo ? "Simulated camera live" : "Camera live" : camera.error ? "Camera needs attention" : videoConnecting ? "Camera connecting" : "Camera offline";
    $("cameraStatusText").title = camera.error || "";
    $("videoFormat").textContent = healthy && video.videoWidth ? `${video.videoWidth} × ${video.videoHeight} · WEBRTC` : "WAITING FOR CAMERA";
    updateControls();
  }

  function drawTarget() {
    const rect = overlay.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.round(rect.width * dpr);
    const height = Math.round(rect.height * dpr);
    if (overlay.width !== width || overlay.height !== height) { overlay.width = width; overlay.height = height; }
    const ctx = overlay.getContext("2d");
    ctx.clearRect(0, 0, width, height);
    const target = state.target || {};
    if (!freshVideo() || !target.valid || !finite(target.x) || !finite(target.y) || !finite(target.radius)) return;
    const imageRatio = video.videoWidth && video.videoHeight ? video.videoWidth / video.videoHeight : 16 / 9;
    const drawWidth = Math.min(width, height * imageRatio);
    const drawHeight = drawWidth / imageRatio;
    const left = (width - drawWidth) / 2;
    const top = (height - drawHeight) / 2;
    const x = left + clamp(target.x, 0, 1) * drawWidth;
    const y = top + clamp(target.y, 0, 1) * drawHeight;
    const radius = Math.max(10 * dpr, target.radius * drawWidth);
    const size = radius + 6 * dpr;
    const segment = Math.min(14 * dpr, size / 2);
    ctx.strokeStyle = "#e9b75e";
    ctx.lineWidth = 1.5 * dpr;
    for (const [dx, dy] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
      ctx.beginPath();
      ctx.moveTo(x + dx * size, y + dy * (size - segment));
      ctx.lineTo(x + dx * size, y + dy * size);
      ctx.lineTo(x + dx * (size - segment), y + dy * size);
      ctx.stroke();
    }
    const label = finite(target.confidence) ? `BALL · ${Math.round(clamp(target.confidence, 0, 1) * 100)}%` : "BALL";
    ctx.font = `${9 * dpr}px Consolas, monospace`;
    const textWidth = ctx.measureText(label).width;
    const labelX = clamp(x - size, 5 * dpr, width - textWidth - 14 * dpr);
    const labelY = Math.max(30 * dpr, y - size - 9 * dpr);
    ctx.fillStyle = "#151b12dd";
    ctx.fillRect(labelX - 4 * dpr, labelY - 11 * dpr, textWidth + 8 * dpr, 16 * dpr);
    ctx.fillStyle = "#e9b75e";
    ctx.fillText(label, labelX, labelY);
  }

  async function bootstrap() {
    const generation = ++bootstrapGeneration;
    try {
      const result = await api("/api/status");
      if (generation !== bootstrapGeneration || retired) return;
      authenticated = result.authenticated === true || result.demo === true;
      $("demoBadge").hidden = result.demo !== true;
      if (!authenticated) {
        setConnection(false, "Access key required");
        if (!$("loginDialog").open) $("loginDialog").showModal();
        updateControls();
        return;
      }
      if ($("loginDialog").open) $("loginDialog").close();
      if (result.state) renderState({...result.state, demo: result.demo});
      connectSocket();
      connectVideo();
    } catch (error) {
      authenticated = false;
      setConnection(false, "Rover unavailable");
      if (error.status === 401) {
        if (!$("loginDialog").open) $("loginDialog").showModal();
      } else showNotice(`Could not reach the rover: ${error.message}`, true);
      updateControls();
    }
  }

  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("loginButton").disabled = true;
    $("loginError").hidden = true;
    try {
      await api("/api/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({token: $("accessKey").value})});
      $("accessKey").value = "";
      await bootstrap();
    } catch (error) {
      $("loginError").textContent = error.message;
      $("loginError").hidden = false;
    } finally { $("loginButton").disabled = false; }
  });
  $("loginDialog").addEventListener("cancel", (event) => event.preventDefault());

  $("enableButton").addEventListener("click", () => {
    if (localEnabled) {
      clearInput();
      send({type: "action", action: "disable"}, {urgent: true});
      localEnabled = false;
      enableRequested = false;
    } else if (freshVideo()) {
      enableRequested = send({type: "action", action: "enable"});
      if (!enableRequested) showNotice("The control link is not ready. Try again.", true);
    }
    updateControls();
  });

  $("grabButton").addEventListener("click", () => {
    clearInput();
    if (localEnabled && freshVideo() && !autonomousPhases.has(currentPhase()) && state.grip_closed !== true) send({type: "action", action: "grab"});
  });
  $("releaseButton").addEventListener("click", () => {
    clearInput();
    send({type: "action", action: "release"}, {urgent: true});
  });
  $("stopButton").addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.preventDefault();
    stopRover();
  });
  // Keyboard and assistive activation emit click without a pointer down.
  $("stopButton").addEventListener("click", (event) => { if (event.detail === 0) stopRover(); });

  for (const button of padButtons) {
    button.addEventListener("pointerdown", (event) => {
      if (button.disabled || (event.pointerType === "mouse" && event.button !== 0)) return;
      event.preventDefault();
      pointers.set(event.pointerId, button.dataset.drive);
      try { button.setPointerCapture(event.pointerId); } catch (_) { /* Global release handler remains active. */ }
      driveTick();
    });
    button.addEventListener("lostpointercapture", (event) => {
      if (pointers.delete(event.pointerId)) driveTick();
    });
    button.addEventListener("contextmenu", (event) => event.preventDefault());
  }
  const releasePointer = (event) => { if (pointers.delete(event.pointerId)) driveTick(); };
  window.addEventListener("pointerup", releasePointer);
  window.addEventListener("pointercancel", releasePointer);
  function typingTarget(event) { return $("loginDialog").open || event.target.matches("input, textarea, select, [contenteditable=true]"); }
  window.addEventListener("keydown", (event) => {
    if (typingTarget(event)) return;
    if (event.code === "Space") {
      event.preventDefault();
      if (!event.repeat) stopRover();
    } else if (keyDirections[event.code] && localEnabled && freshVideo()) {
      event.preventDefault();
      if (!event.repeat) { keys.add(event.code); driveTick(); }
    }
  });
  window.addEventListener("keyup", (event) => {
    if (keys.delete(event.code)) { event.preventDefault(); driveTick(); }
  });
  $("speedLimit").addEventListener("input", () => { $("speedValue").textContent = `${$("speedLimit").value}%`; driveTick(); });

  $("reconnectButton").addEventListener("click", async () => {
    pauseForFocusLoss();
    closeVideo();
    clearTimeout(reconnectTimer);
    if (socket) { const old = socket; socket = null; old.close(); }
    ticket = null;
    await bootstrap();
  });
  $("fullscreenButton").addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if ($("videoStage").requestFullscreen) await $("videoStage").requestFullscreen();
      else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
    } catch (_) { showNotice("Fullscreen is unavailable in this browser."); }
  });
  window.addEventListener("blur", pauseForFocusLoss);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { pauseForFocusLoss(); closeVideo(); }
    else { connectSocket(); connectVideo(); }
  });
  window.addEventListener("pagehide", () => { pauseForFocusLoss(); closeVideo(); });
  window.addEventListener("pageshow", () => { if (authenticated) { connectSocket(); connectVideo(); } });
  window.addEventListener("beforeunload", () => { pauseForFocusLoss(); retired = true; closeVideo(); if (socket) socket.close(); });
  window.addEventListener("resize", drawTarget);

  setInterval(driveTick, 40);
  setInterval(() => {
    renderVideoHealth();
    if (isSocketOpen()) send({type: "heartbeat", seq: ++seq, video_ok: freshVideo()});
  }, 150);
  setInterval(() => { if (isSocketOpen()) send({type: "ping", sent_at: performance.now()}); }, 1000);
  // Browsers without requestVideoFrameCallback use 250 ms decoded-frame deltas.
  setInterval(pollVideoStats, 250);
  watchRenderedFrames();
  bootstrap();
})();
