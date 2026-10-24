(() => {
  const view = document.querySelector("[data-conversation-id]");
  if (!view) return;

  const conversationId = view.dataset.conversationId;
  const currentUserId = view.dataset.currentUserId;
  const messageList = document.querySelector("#message-list");
  const messageForm = document.querySelector("#message-form");
  const messageError = document.querySelector("#message-error");
  const connectionState = document.querySelector("#chat-connection-state");
  const attachmentInput = document.querySelector("#message-attachments");
  const attachmentProgress = document.querySelector("#attachment-progress");
  let socket;
  let reconnectDelay = 1000;

  // One key per logical request: a retry after a lost response replays the stored outcome
  // instead of creating a second record. The fallback covers origins where
  // crypto.randomUUID is missing because the page is not a secure context.
  function newIdempotencyKey() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }

  // FastAPI reports validation problems as an array of objects; a bare array in
  // new Error(...) renders as "[object Object]".
  function errorMessage(error, fallback) {
    if (Array.isArray(error.detail)) {
      return error.detail.map((item) => item.msg).filter(Boolean).join("; ") || fallback;
    }
    return error.detail || fallback;
  }

  function appendMessage(message) {
    if (messageList.querySelector(`[data-message-id="${message.id}"]`)) return;
    const wasNearLatest = isNearLatest();
    const row = document.createElement("article");
    row.className = `message-row ${message.sender_id === currentUserId ? "message-mine" : "message-theirs"}`;
    row.dataset.messageId = message.id;
    row.dataset.senderId = message.sender_id;
    if (message.sender_id !== currentUserId && view.dataset.kind === "group") {
      const sender = document.createElement("span");
      sender.className = "message-sender";
      sender.textContent = message.sender_name;
      row.append(sender);
    }
    if (message.body) {
      const bubble = document.createElement("p");
      bubble.className = "message-bubble";
      bubble.textContent = message.body;
      row.append(bubble);
    }
    renderAttachments(row, message.attachments);
    const time = document.createElement("time");
    time.dateTime = message.created_at;
    time.textContent = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(message.created_at));
    row.append(time);
    messageList.querySelector(".message-empty")?.remove();
    messageList.append(row);
    if (wasNearLatest) scrollToLatest();
    if (message.sender_id !== currentUserId) markRead();
  }

  async function markRead() {
    const token = messageForm?.elements.namedItem("csrf_token")?.value;
    if (!token) return;
    const data = new FormData();
    data.set("csrf_token", token);
    const response = await fetch(`/conversations/${conversationId}/read`, { method: "POST", body: data });
    // The shell badge counts every conversation, so let it ask for the new total rather than
    // guess how much this one held.
    if (response.ok) document.dispatchEvent(new CustomEvent("chat:read"));
  }

  // A delivered attachment leaves the server: once every other participant has the bytes, the
  // stored object goes. Each reader therefore keeps their own copy in the browser, which is
  // what lets an expired bubble still open the file they received. The store is named per
  // account so a second sign-in on the same machine cannot read the first one's files.
  const attachmentCache = (() => {
    const DB_NAME = `social-attachments-${view.dataset.currentUserId}`;
    const STORE = "attachments";
    const LIMIT = 200;
    let opening;

    function open() {
      if (!("indexedDB" in window)) return Promise.resolve(null);
      opening ||= new Promise((resolve) => {
        const request = indexedDB.open(DB_NAME, 1);
        request.onupgradeneeded = () => {
          const store = request.result.createObjectStore(STORE, { keyPath: "id" });
          store.createIndex("cachedAt", "cachedAt");
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => resolve(null);
      });
      return opening;
    }

    function run(mode, work) {
      return open().then(
        (database) =>
          new Promise((resolve) => {
            if (!database) {
              resolve(null);
              return;
            }
            const transaction = database.transaction(STORE, mode);
            const request = work(transaction.objectStore(STORE));
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => resolve(null);
          })
      );
    }

    return {
      get: (id) => run("readonly", (store) => store.get(id)),
      async put(record) {
        const key = await run("readwrite", (store) => store.put(record));
        if (key === null || key === undefined) return false;
        // Keep the local copy bounded: attachments are the largest thing a page stores.
        await run("readwrite", (store) => {
          const request = store.index("cachedAt").openCursor();
          let seen = 0;
          request.onsuccess = () => {
            const cursor = request.result;
            if (!cursor) return;
            seen += 1;
            if (seen > LIMIT) cursor.delete();
            cursor.continue();
          };
          return request;
        });
        return true;
      },
    };
  })();

  const attachmentUrls = new Map();

  function objectUrl(record) {
    if (!attachmentUrls.has(record.id)) {
      attachmentUrls.set(record.id, URL.createObjectURL(record.blob));
    }
    return attachmentUrls.get(record.id);
  }

  function hydrateFromCache(element, record) {
    const link = document.createElement("a");
    link.className = "message-attachment";
    link.href = objectUrl(record);
    link.title = "Saved on this device";
    if (record.inline) {
      link.target = "_blank";
      link.rel = "noopener";
      const image = document.createElement("img");
      image.src = link.href;
      image.alt = record.filename;
      link.append(image);
    } else {
      link.download = record.filename;
      link.textContent = `${record.filename} · ${Math.round(record.size / 1024)} KB`;
    }
    element.replaceWith(link);
  }

  function showFromCache(element, record) {
    if (record.inline) {
      const image = element.querySelector("img[data-attachment-image]");
      if (image) image.src = objectUrl(record);
      element.href = objectUrl(record);
      return;
    }
    element.href = objectUrl(record);
    element.download = record.filename;
  }

  function acknowledgeDelivery(id) {
    const token = messageForm?.elements.namedItem("csrf_token")?.value;
    if (!token) return;
    const data = new FormData();
    data.set("csrf_token", token);
    // Confirmation, not the download itself, is what lets the server release its copy: this
    // device has the bytes stored, so the file survives for this reader.
    fetch(`/attachments/${id}/received`, { method: "POST", body: data }).catch(() => {});
  }

  async function cacheAttachment(element) {
    const id = element.dataset.attachmentId;
    if (!id) return null;
    const cached = await attachmentCache.get(id);
    if (cached) {
      if (element.dataset.expired !== "true") acknowledgeDelivery(id);
      return cached;
    }
    if (element.dataset.expired === "true") return null;
    const response = await fetch(element.dataset.attachmentSrc, { credentials: "same-origin" });
    if (!response.ok) return null;
    const blob = await response.blob();
    const record = {
      id,
      blob,
      filename: element.dataset.attachmentName,
      inline: element.dataset.attachmentInline === "true",
      size: Number(element.dataset.attachmentSize) || blob.size,
      cachedAt: Date.now(),
    };
    const stored = await attachmentCache.put(record);
    if (!stored) return record;
    acknowledgeDelivery(id);
    return record;
  }

  async function prepareAttachment(element) {
    if (element.dataset.expired === "true") {
      const record = await attachmentCache.get(element.dataset.attachmentId);
      if (record) hydrateFromCache(element, record);
      return;
    }
    const preview = element.dataset.attachmentThumb;
    if (preview) {
      // A preview is the whole point of the derivative: the original stays on the server
      // until the reader clicks, which is also when the local copy is made.
      const image = element.querySelector("img[data-attachment-image]");
      if (image) {
        image.src = preview;
        holdReadingPosition();
      }
      return;
    }
    // No derivative (a small image, or one written before derivatives existed): the original
    // is small enough to render, and fetching it is the old behaviour.
    const record = await cacheAttachment(element);
    if (record) showFromCache(element, record);
    holdReadingPosition();
  }

  // Attachment bytes are the largest thing a conversation holds, so a preview is fetched when
  // it approaches the viewport instead of when the message is rendered. A page of history
  // therefore costs what the reader actually looks at, not what it contains.
  const attachmentObserver =
    typeof IntersectionObserver === "function"
      ? new IntersectionObserver(
          (entries) => {
            for (const entry of entries) {
              if (!entry.isIntersecting) continue;
              attachmentObserver.unobserve(entry.target);
              prepareAttachment(entry.target);
            }
          },
          { rootMargin: "400px 0px" }
        )
      : null;

  function prepareWhenVisible(element) {
    if (!attachmentObserver) {
      prepareAttachment(element);
      return;
    }
    attachmentObserver.observe(element);
  }

  function interceptDownload(element, { open = false } = {}) {
    element.addEventListener("click", async (event) => {
      if (element.dataset.expired === "true") return;
      event.preventDefault();
      const record = await cacheAttachment(element);
      if (!record) {
        window.location.assign(element.dataset.attachmentSrc);
        return;
      }
      const link = document.createElement("a");
      link.href = objectUrl(record);
      if (open) {
        link.target = "_blank";
        link.rel = "noopener";
      } else {
        link.download = record.filename;
      }
      link.click();
    });
  }

  function renderAttachments(row, attachments) {
    if (!attachments?.length) return;
    const container = document.createElement("div");
    container.className = "message-attachments";
    attachments.forEach((attachment) => {
      const element = document.createElement(attachment.expired ? "span" : "a");
      element.className = `message-attachment${attachment.expired ? " message-attachment-expired" : ""}`;
      element.dataset.attachmentId = attachment.id;
      element.dataset.attachmentSrc = attachment.download_url;
      element.dataset.attachmentName = attachment.filename;
      element.dataset.attachmentInline = String(Boolean(attachment.inline));
      element.dataset.attachmentKind = attachment.kind || "";
      element.dataset.attachmentThumb = attachment.thumbnail_url || "";
      element.dataset.attachmentSize = String(attachment.size_bytes);
      if (attachment.expired) {
        element.dataset.expired = "true";
        element.textContent = `${attachment.filename} · Attachment expired`;
      } else if (attachment.inline && attachment.kind === "image") {
        element.href = attachment.download_url;
        element.target = "_blank";
        element.rel = "noopener";
        const image = document.createElement("img");
        image.dataset.attachmentImage = "";
        image.alt = attachment.filename;
        image.loading = "lazy";
        element.append(image);
        // The preview is cheap, but the click is what stores the original and confirms
        // delivery, so it goes through the same path as any other download.
        interceptDownload(element, { open: true });
      } else {
        // A video or a document is fetched when the reader asks for it, never to render a
        // link: the message list carries the name and the size, and the click is the request.
        element.href = attachment.download_url;
        element.textContent = `${attachment.filename} · ${Math.round(attachment.size_bytes / 1024)} KB`;
        interceptDownload(element);
      }
      container.append(element);
      if (attachment.expired) prepareAttachment(element);
      else if (attachment.inline && attachment.kind === "image") prepareWhenVisible(element);
    });
    row.append(container);
  }

  function refreshAttachmentSummary() {
    if (!attachmentInput) return;
    const count = attachmentInput.files?.length || 0;
    attachmentProgress.textContent = count
      ? `${count} file${count === 1 ? "" : "s"} attached`
      : "";
  }

  async function requestUploadTarget(file, checksum) {
    const response = await fetch(`/conversations/${conversationId}/attachments`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": newIdempotencyKey() },
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type,
        size_bytes: file.size,
        checksum_sha256: checksum || undefined,
      }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(errorMessage(error, "That file was refused."));
    }
    return response.json();
  }

  // The server keeps the digest honest at finalize; this one only lets it answer a repeat
  // upload with a copy of bytes it already holds.
  async function checksumFor(file) {
    const digest = globalThis.SocialMediaOptimize?.sha256;
    return digest ? digest(file) : null;
  }

  async function finalizeUpload(attachmentId) {
    const response = await fetch(`/attachments/${attachmentId}/finalize`, { method: "POST" });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(errorMessage(error, "The upload was rejected."));
    }
    return response.json();
  }

  async function completeUpload(target, file, onProgress) {
    // The bytes are already in storage for this account: nothing to send, nothing to scan.
    if (target.reused) return { id: target.attachment_id };
    const upload = globalThis.SocialMediaOptimize?.uploadFile;
    if (!upload) throw new Error("This browser cannot upload files.");
    const { finalized } = await upload(target, file, onProgress);
    // A split upload is verified when its last part is assembled, so it arrives complete.
    return finalized || finalizeUpload(target.attachment_id);
  }

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${scheme}//${window.location.host}/ws/conversations/${conversationId}`);
    socket.addEventListener("open", async () => {
      reconnectDelay = 1000;
      if (connectionState) connectionState.textContent = "Live";
      // A socket that just came back may have missed the end of the call it was carrying, and a
      // page that just loaded may still owe its reader a ring that started while it was away.
      try {
        const response = await fetch(`/conversations/${conversationId}/calls/current`, {
          credentials: "same-origin",
        });
        const data = await response.json();
        if (data?.call) {
          const mine = participantRow(data.call, currentUserId);
          if (call.id || mine?.state === "invited") handleCallState(data.call);
        } else if (call.id) {
          finishCall("The call ended while the connection was down.");
        }
      } catch {
        // Keep the local state; the next packet settles it.
      }
    });
    socket.addEventListener("message", (event) => {
      if (event.data === "pong") return;
      try {
        const packet = JSON.parse(event.data);
        if (packet.type === "message") appendMessage(packet.message);
        if (packet.type === "call_state") handleCallState(packet.call);
        if (packet.type === "call_signal") handleCallSignal(packet);
        if (packet.type === "call_error") handleCallError(packet);
        if (packet.type === "membership") {
          if (packet.subject_user_id === currentUserId) {
            window.location.assign("/conversations");
          } else {
            window.location.reload();
          }
        }
      } catch {
        return;
      }
    });
    socket.addEventListener("close", () => {
      if (connectionState) connectionState.textContent = "Reconnecting…";
      // Signaling needs the socket, so a dropped connection ends whatever call was running
      // rather than leaving a dead peer connection behind.
      if (call.id) finishCall("The connection dropped, so the call ended.");
      window.setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    });
    socket.addEventListener("error", () => socket.close());
  }

  // Calls.
  //
  // A call is a mesh: the server records who was invited and who joined, then relays SDP and
  // ICE between every pair of joined participants. Media itself is peer-to-peer under
  // DTLS-SRTP, so no audio or video reaches the application and no call is recorded. On each
  // pair the participant with the lower user id offers, which makes negotiation deterministic
  // without a glare protocol.
  const callPanel = document.querySelector("#call-panel");
  const callStatus = document.querySelector("#call-status");
  const callDetail = document.querySelector("#call-detail");
  const callRemotes = document.querySelector("#call-remotes");
  const callSelfTile = document.querySelector("#call-self-tile");
  const callSelfVideo = document.querySelector("#call-self-video");
  const callSafety = document.querySelector("#call-safety");
  const callSafetyNote = document.querySelector("#call-safety-note");
  const callSafetyList = document.querySelector("#call-safety-list");
  const callAnswer = document.querySelector("#call-answer");
  const callDecline = document.querySelector("#call-decline");
  const callMute = document.querySelector("#call-mute");
  const callMuteIcon = document.querySelector("#call-mute-icon");
  const callCamera = document.querySelector("#call-camera");
  const callCameraIcon = document.querySelector("#call-camera-icon");
  const callCameraLabel = document.querySelector("#call-camera-label");
  const callHangup = document.querySelector("#call-hangup");
  const callHangupLabel = document.querySelector("#call-hangup-label");
  const startAudioCall = document.querySelector("#start-audio-call");
  const startVideoCall = document.querySelector("#start-video-call");

  const call = {
    id: null,
    media: "audio",
    state: "idle",
    payload: null,
    localStream: null,
    iceServers: null,
    peers: new Map(),
    muted: false,
    cameraOn: false,
    timer: null,
    statsTimer: null,
    hideTimer: null,
    degradedSamples: 0,
    // The current outbound video ceiling, lowered when the link cannot carry it.
    videoMaxBitrate: 1200000,
    videoDegraded: false,
  };

  const CALL_END_TEXT = {
    hangup: "Call ended.",
    cancelled: "Call cancelled.",
    declined: "Call declined.",
    missed: "Call missed.",
    failed: "The call could not connect.",
    busy: "The call was busy.",
  };

  const CALL_ERROR_TEXT = {
    unavailable: "The call is no longer available.",
    blocked: "This call is not allowed.",
    "rate-limited": "Too many call signals. Wait a moment and try again.",
    "group-calls-unavailable": "Group calls are not available.",
  };

  // What a call is allowed to spend. Audio is a narrowband conversation, so 32 kbit/s covers
  // Opus at full quality; video scales down as the mesh grows, because in a mesh every
  // participant uploads to every other one and the uplink is the scarce side of the link.
  const AUDIO_MAX_BITRATE = 32000;
  const VIDEO_MIN_BITRATE = 150000;
  const VIDEO_BITRATE_BY_PEERS = { 1: 1200000, 2: 700000 };
  const VIDEO_GROUP_BITRATE = 400000;

  function videoBitrateFor(peers) {
    return VIDEO_BITRATE_BY_PEERS[peers] || VIDEO_GROUP_BITRATE;
  }

  // Opus carries two knobs that matter for a call: DTX stops sending frames during silence
  // instead of spending the full bitrate on nothing, and in-band FEC repairs loss without a
  // retransmission. Both live in the fmtp line of the negotiation this page produces, so the
  // browser's own encoder budget is bounded the same way the sender is.
  function preferLowBandwidthOpus(sdp) {
    if (!sdp) return sdp;
    const payloadType = /a=rtpmap:(\d+) opus\/48000(?:\/\d+)?/i.exec(sdp)?.[1];
    if (!payloadType) return sdp;
    const line = new RegExp(`a=fmtp:${payloadType} ([^\\r\\n]*)`);
    const match = line.exec(sdp);
    if (!match) return sdp;
    const params = new Map();
    for (const part of match[1].split(";")) {
      const [key, value] = part.split("=");
      if (key?.trim()) params.set(key.trim(), (value ?? "").trim());
    }
    params.set("stereo", "0");
    params.set("usedtx", "1");
    params.set("useinbandfec", "1");
    params.set("maxaveragebitrate", String(AUDIO_MAX_BITRATE));
    const fmtp = `a=fmtp:${payloadType} ${Array.from(params, ([key, value]) => `${key}=${value}`).join(";")}`;
    return sdp.replace(line, () => fmtp);
  }

  // The ceiling is applied to the sender rather than trusted to congestion control: without
  // it, a video encoder happily fills whatever the link will take.
  function capSenders(connection) {
    for (const sender of connection.getSenders()) {
      const kind = sender.track?.kind;
      if (kind !== "audio" && kind !== "video") continue;
      let params;
      try {
        params = sender.getParameters();
      } catch {
        continue;
      }
      if (!params.encodings?.length) params.encodings = [{}];
      const encoding = params.encodings[0];
      if (kind === "audio") {
        encoding.maxBitrate = AUDIO_MAX_BITRATE;
      } else {
        encoding.maxBitrate = call.videoMaxBitrate;
        encoding.maxFramerate = 24;
        params.degradationPreference = "balanced";
        sender.track.contentHint = "motion";
      }
      sender.setParameters(params).catch(() => {
        // A browser with partial `setParameters` support still accepts the bitrate ceiling,
        // which is the part that decides how many bytes leave this machine.
        let minimal;
        try {
          minimal = sender.getParameters();
        } catch {
          return;
        }
        if (!minimal.encodings?.length) minimal.encodings = [{}];
        minimal.encodings[0].maxBitrate =
          kind === "audio" ? AUDIO_MAX_BITRATE : call.videoMaxBitrate;
        sender.setParameters(minimal).catch(() => {});
      });
    }
  }

  function capAllSenders() {
    for (const peerState of call.peers.values()) capSenders(peerState.connection);
  }

  function participantRow(payload, userId) {
    return (payload?.participants || []).find((row) => row.user_id === userId) || null;
  }

  function otherParticipants(payload, state) {
    return (payload?.participants || []).filter(
      (row) => row.user_id !== currentUserId && row.state === state
    );
  }

  function displayNames(rows) {
    return rows.map((row) => row.display_name).join(", ");
  }

  function setCallButtons({
    answer = false,
    decline = false,
    mute = false,
    camera = false,
    hangup = false,
    hangupLabel = "End call",
  } = {}) {
    if (callAnswer) callAnswer.hidden = !answer;
    if (callDecline) callDecline.hidden = !decline;
    if (callMute) callMute.hidden = !mute;
    if (callCamera) callCamera.hidden = !camera;
    if (callHangup) callHangup.hidden = !hangup;
    if (callHangupLabel) callHangupLabel.textContent = hangupLabel;
    const busy = call.state !== "idle";
    if (startAudioCall) startAudioCall.disabled = busy;
    if (startVideoCall) startVideoCall.disabled = busy;
  }

  function showPanel(status, detail, buttons) {
    if (!callPanel) return;
    window.clearTimeout(call.hideTimer);
    call.hideTimer = null;
    callPanel.hidden = false;
    callStatus.textContent = status || "";
    if (callDetail) {
      callDetail.textContent = detail || "";
      callDetail.hidden = !detail;
    }
    setCallButtons(buttons);
  }

  async function loadIceServers() {
    try {
      const response = await fetch("/calls/ice", { credentials: "same-origin" });
      if (response.ok) {
        const data = await response.json();
        if (Array.isArray(data.ice_servers) && data.ice_servers.length) return data.ice_servers;
      }
    } catch {
      // Fall through to the page's own list: a call without TURN still works on most networks.
    }
    return (view.dataset.iceServers || "")
      .split(",")
      .map((url) => url.trim())
      .filter(Boolean)
      .map((urls) => ({ urls }));
  }

  function mediaErrorMessage(error, wantsVideo) {
    const devices = wantsVideo ? "camera and microphone" : "microphone";
    switch (error?.name) {
      case "NotAllowedError":
      case "SecurityError":
        return `Access to your ${devices} is blocked. Allow it in the browser and try again.`;
      case "NotFoundError":
      case "OverconstrainedError":
        return `No ${devices} was available for this call.`;
      case "NotReadableError":
        return `Your ${devices} is already in use by another application.`;
      default:
        return `The ${devices} could not be opened.`;
    }
  }

  async function openLocalMedia(media) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("This browser does not support calls.");
    }
    const wantsVideo = media === "video";
    try {
      return await navigator.mediaDevices.getUserMedia({
        // Speech in one channel at 48 kHz is what Opus encodes efficiently; stereo doubles
        // the audio budget for nothing and the encoder averages the channels back down.
        audio: {
          channelCount: { ideal: 1 },
          sampleRate: { ideal: 48000 },
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        // 720p is the useful ceiling for a conversation; the sender caps below decide how
        // much of that is actually transmitted.
        video: wantsVideo
          ? {
              width: { ideal: 1280 },
              height: { ideal: 720 },
              frameRate: { ideal: 30, max: 30 },
              facingMode: "user",
            }
          : false,
      });
    } catch (error) {
      throw new Error(mediaErrorMessage(error, wantsVideo));
    }
  }

  async function callRequest(path, options = {}) {
    const response = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(errorMessage(error, "The call request was refused."));
    }
    return response.json();
  }

  function sendSignal(toUserId, signal, payload = {}) {
    if (!call.id || socket?.readyState !== WebSocket.OPEN) return false;
    socket.send(
      JSON.stringify({
        type: "call_signal",
        call_id: call.id,
        to_user_id: toUserId,
        signal,
        payload,
      })
    );
    return true;
  }

  function formatDuration(totalSeconds) {
    const minutes = Math.floor(totalSeconds / 60).toString().padStart(2, "0");
    const seconds = (totalSeconds % 60).toString().padStart(2, "0");
    return `${minutes}:${seconds}`;
  }

  function updateTimerText() {
    if (!call.payload || call.payload.state !== "accepted") return;
    const answered = Date.parse(call.payload.answered_at || "");
    if (Number.isNaN(answered)) return;
    const seconds = Math.max(0, Math.floor((Date.now() - answered) / 1000));
    callStatus.textContent = `In call · ${formatDuration(seconds)}`;
  }

  function startTimer() {
    window.clearInterval(call.timer);
    const tick = () => updateTimerText();
    call.timer = window.setInterval(tick, 1000);
    tick();
  }

  function showMedia(video) {
    if (callRemotes) callRemotes.hidden = false;
    if (callSelfTile) callSelfTile.hidden = !video;
    if (video && callSelfVideo) {
      callSelfVideo.srcObject = call.localStream;
      callSelfVideo.play?.().catch(() => {});
    }
  }

  function buildVideoTile(peer) {
    const tile = document.createElement("div");
    tile.className = "call-tile";
    tile.dataset.userId = peer.user_id;
    const media = document.createElement("video");
    media.autoplay = true;
    media.playsInline = true;
    const label = document.createElement("span");
    label.className = "call-tile-label";
    label.textContent = peer.display_name;
    tile.append(media, label);
    return { tile, media };
  }

  function attachRemote(peer, peerState, stream) {
    if (call.media === "video") {
      if (!peerState.mediaElement) {
        const { tile, media } = buildVideoTile(peer);
        callRemotes?.append(tile);
        peerState.tile = tile;
        peerState.mediaElement = media;
      }
      peerState.mediaElement.srcObject = stream;
      peerState.mediaElement.play?.().catch(() => {});
      return;
    }
    if (!peerState.mediaElement) {
      const audio = document.createElement("audio");
      audio.autoplay = true;
      audio.playsInline = true;
      callRemotes?.append(audio);
      peerState.mediaElement = audio;
    }
    peerState.mediaElement.srcObject = stream;
    peerState.mediaElement.play?.().catch(() => {});
  }

  async function updateSafetyNumbers() {
    if (!callSafety || !callSafetyNote) return;
    const derivation = globalThis.SocialSafetyNumber;
    const entries = [];
    if (derivation) {
      for (const [userId, peerState] of call.peers) {
        const connection = peerState.connection;
        // Both sides sort the two DTLS fingerprints and hash them, so a key substituted during
        // signaling produces a different number: comparing it out of band is what makes the
        // encryption claim checkable rather than asserted.
        const digits = await derivation.safetyNumber(
          connection.localDescription?.sdp,
          connection.remoteDescription?.sdp
        );
        if (digits) entries.push({ userId, digits });
      }
    }
    if (!entries.length) {
      callSafety.hidden = true;
      callSafetyNote.textContent = "";
      if (callSafetyList) {
        callSafetyList.hidden = true;
        callSafetyList.replaceChildren();
      }
      return;
    }
    entries.sort((first, second) => first.userId.localeCompare(second.userId));
    if (entries.length === 1 && call.payload?.kind === "direct") {
      const other = otherParticipants(call.payload, "joined")[0];
      callSafetyNote.textContent =
        `Safety number ${derivation.format(entries[0].digits)}. Compare it with ` +
        `${other?.display_name || "the other person"} out of band to confirm the call is not ` +
        "passing through anyone else.";
      if (callSafetyList) {
        callSafetyList.hidden = true;
        callSafetyList.replaceChildren();
      }
    } else {
      // Every pair in a group call has its own DTLS keys, so each one gets its own number.
      callSafetyNote.textContent =
        "Each connection in this call is encrypted separately. Compare each number with that " +
        "person out of band:";
      if (callSafetyList) {
        callSafetyList.hidden = false;
        callSafetyList.replaceChildren(
          ...entries.map(({ userId, digits }) => {
            const row = (call.payload?.participants || []).find(
              (participant) => participant.user_id === userId
            );
            const item = document.createElement("li");
            item.textContent = `${row?.display_name || "Participant"} · ${derivation.format(digits)}`;
            return item;
          })
        );
      }
    }
    callSafety.hidden = false;
  }

  async function drainCandidates(peerState) {
    const queued = peerState.candidates;
    peerState.candidates = [];
    for (const candidate of queued) {
      await peerState.connection.addIceCandidate(candidate).catch(() => {});
    }
  }

  function notePeerFailure(userId) {
    closePeer(userId);
    if (call.payload?.kind === "direct") {
      endActiveCall("failed");
      finishCall("The call could not connect.");
      return;
    }
    if (callDetail) {
      callDetail.textContent = "A participant lost their connection.";
      callDetail.hidden = false;
    }
  }

  function createPeer(peer) {
    const connection = new RTCPeerConnection({ iceServers: call.iceServers || [] });
    const peerState = {
      connection,
      candidates: [],
      remoteDescriptionSet: false,
      mediaElement: null,
      tile: null,
      audioSender: null,
      videoSender: null,
    };
    call.peers.set(peer.user_id, peerState);
    if (call.localStream) {
      call.localStream.getTracks().forEach((track) => {
        const sender = connection.addTrack(track, call.localStream);
        // Remembered by kind because a sender whose track has been replaced with `null` no
        // longer answers that question.
        if (track.kind === "video") peerState.videoSender = sender;
        else if (track.kind === "audio") peerState.audioSender = sender;
      });
    }
    // A peer that joins while the microphone or camera is off must not receive that track
    // just because it was added with the others.
    if (call.muted) peerState.audioSender?.replaceTrack(null).catch(() => {});
    if (!call.cameraOn) peerState.videoSender?.replaceTrack(null).catch(() => {});
    connection.onicecandidate = (event) => {
      if (event.candidate) {
        sendSignal(peer.user_id, "ice", { candidate: event.candidate.toJSON() });
      }
    };
    connection.ontrack = (event) => {
      attachRemote(peer, peerState, event.streams[0] || new MediaStream([event.track]));
    };
    connection.onconnectionstatechange = () => {
      if (connection.connectionState === "failed") notePeerFailure(peer.user_id);
      // The ceiling can only be applied once the sender has a negotiated encoding, which is
      // also the first moment there are bytes worth bounding.
      if (connection.connectionState === "connected") capSenders(connection);
    };
    if (currentUserId < peer.user_id) {
      negotiate(peerState, peer.user_id);
    }
    return peerState;
  }

  async function negotiate(peerState, userId) {
    try {
      const offer = await peerState.connection.createOffer();
      offer.sdp = preferLowBandwidthOpus(offer.sdp);
      await peerState.connection.setLocalDescription(offer);
      capSenders(peerState.connection);
      const description = peerState.connection.localDescription;
      sendSignal(userId, "offer", { type: description.type, sdp: description.sdp });
    } catch {
      notePeerFailure(userId);
    }
  }

  function closePeer(userId) {
    const peerState = call.peers.get(userId);
    if (!peerState) return;
    peerState.connection.onicecandidate = null;
    peerState.connection.ontrack = null;
    peerState.connection.onconnectionstatechange = null;
    peerState.connection.close();
    peerState.mediaElement?.remove();
    peerState.tile?.remove();
    call.peers.delete(userId);
  }

  function syncPeers() {
    if (!call.payload || call.state !== "in-call") return;
    const mine = participantRow(call.payload, currentUserId);
    if (!mine || mine.state !== "joined") return;
    const joined = otherParticipants(call.payload, "joined");
    const wanted = new Set(joined.map((peer) => peer.user_id));
    for (const userId of Array.from(call.peers.keys())) {
      if (!wanted.has(userId)) closePeer(userId);
    }
    if (call.payload.state === "accepted") {
      for (const peer of joined) {
        if (!call.peers.has(peer.user_id)) createPeer(peer);
      }
    }
    // A mesh sends one stream to every peer, so the per-stream ceiling follows the
    // participant count: a group call keeps roughly the uplink of a 1:1 call instead of
    // multiplying it by the number of people in the room.
    const ceiling = videoBitrateFor(Math.max(joined.length, 1));
    if (!call.videoDegraded && ceiling !== call.videoMaxBitrate) {
      call.videoMaxBitrate = ceiling;
      capAllSenders();
    }
    updateSafetyNumbers();
  }

  function renderCall() {
    if (!call.payload) return;
    const payload = call.payload;
    const mine = participantRow(payload, currentUserId);
    const label = payload.media === "video" ? "Video call" : "Audio call";
    if (payload.state === "ended") {
      finishCall(CALL_END_TEXT[payload.end_reason] || "Call ended.");
      return;
    }
    if (!mine) {
      finishCall("");
      return;
    }
    if (mine.state === "invited") {
      const initiator = participantRow(payload, payload.initiator_user_id);
      call.state = "ringing";
      showPanel(`${initiator?.display_name || "Someone"} is calling`, label, {
        answer: true,
        decline: true,
      });
      return;
    }
    if (mine.state !== "joined") {
      finishCall("You have left the call.");
      return;
    }

    call.state = "in-call";
    const others = otherParticipants(payload, "joined");
    const invited = otherParticipants(payload, "invited");
    if (payload.state === "ringing") {
      const waiting = displayNames(invited) || displayNames(others);
      showPanel(`Calling ${waiting || "…"}…`, label, {
        hangup: true,
        hangupLabel: "Cancel",
      });
      return;
    }
    const names = displayNames(others);
    showPanel("", names ? `${label} · ${names}` : label, {
      mute: true,
      camera: payload.media === "video",
      hangup: true,
      hangupLabel: payload.kind === "group" && others.length ? "Leave call" : "End call",
    });
    setMuted(call.muted);
    setCameraEnabled(call.cameraOn);
    showMedia(payload.media === "video");
    startTimer();
  }

  function enterCall(payload) {
    call.payload = payload;
    call.id = payload.id;
    call.media = payload.media;
    call.degradedSamples = 0;
    call.videoMaxBitrate = videoBitrateFor(1);
    call.videoDegraded = false;
    renderCall();
    syncPeers();
    startStatsMonitor();
  }

  function handleCallState(payload) {
    if (!payload?.id) return;
    if (call.id && payload.id !== call.id) return;
    const mine = participantRow(payload, currentUserId);
    if (!mine) return;
    if (payload.state === "ended") {
      if (payload.id === call.id || call.state === "ringing") {
        finishCall(CALL_END_TEXT[payload.end_reason] || "Call ended.");
      }
      return;
    }
    call.payload = payload;
    call.id = payload.id;
    call.media = payload.media;
    renderCall();
    syncPeers();
  }

  async function handleCallSignal(packet) {
    if (packet.call_id !== call.id) return;
    const peerState = call.peers.get(packet.from_user_id);
    if (!peerState) return;
    const connection = peerState.connection;
    try {
      if (packet.signal === "offer") {
        await connection.setRemoteDescription({ type: "offer", sdp: packet.payload?.sdp });
        peerState.remoteDescriptionSet = true;
        await drainCandidates(peerState);
        const answer = await connection.createAnswer();
        answer.sdp = preferLowBandwidthOpus(answer.sdp);
        await connection.setLocalDescription(answer);
        capSenders(connection);
        const description = connection.localDescription;
        sendSignal(packet.from_user_id, "answer", {
          type: description.type,
          sdp: description.sdp,
        });
        updateSafetyNumbers();
      } else if (packet.signal === "answer") {
        await connection.setRemoteDescription({ type: "answer", sdp: packet.payload?.sdp });
        peerState.remoteDescriptionSet = true;
        await drainCandidates(peerState);
        updateSafetyNumbers();
      } else if (packet.signal === "ice" && packet.payload?.candidate) {
        if (peerState.remoteDescriptionSet) {
          await connection.addIceCandidate(packet.payload.candidate).catch(() => {});
        } else {
          // Candidates can arrive before the description they belong to.
          peerState.candidates.push(packet.payload.candidate);
        }
      }
    } catch {
      notePeerFailure(packet.from_user_id);
    }
  }

  function handleCallError(packet) {
    if (packet.call_id !== call.id) return;
    const text = CALL_ERROR_TEXT[packet.reason] || "The call could not continue.";
    if (packet.reason === "unavailable") {
      endActiveCall("failed");
      finishCall(text);
      return;
    }
    showPanel(callStatus.textContent, text, {
      mute: !callMute?.hidden,
      camera: !callCamera?.hidden,
      hangup: !callHangup?.hidden,
      hangupLabel: callHangupLabel?.textContent || "End call",
    });
  }

  function setMuted(muted) {
    call.muted = muted;
    const track = call.localStream?.getAudioTracks()[0] || null;
    if (track) track.enabled = !muted;
    // A muted track still sends silence. Replacing it with nothing stops the RTP stream
    // until the microphone button is pressed again, which is the only way to spend zero
    // bytes while muted.
    if (track) {
      for (const peerState of call.peers.values()) {
        peerState.audioSender?.replaceTrack(muted ? null : track).catch(() => {});
      }
    }
    if (callMute) callMute.setAttribute("aria-pressed", muted ? "true" : "false");
    if (callMuteIcon) callMuteIcon.textContent = muted ? "mic_off" : "mic";
  }

  function setCameraEnabled(enabled) {
    call.cameraOn = enabled;
    const track = call.localStream?.getVideoTracks()[0] || null;
    if (track) track.enabled = enabled;
    // A disabled video track still encodes black frames and still costs an RTP stream, so
    // the camera is detached from every sender rather than merely paused.
    for (const peerState of call.peers.values()) {
      peerState.videoSender
        ?.replaceTrack(enabled ? track : null)
        // Restoring the track also restores the ceiling: a sender that had no track when the
        // call was negotiated never received one.
        .then(() => capSenders(peerState.connection))
        .catch(() => {});
    }
    if (callCameraIcon) callCameraIcon.textContent = enabled ? "videocam" : "videocam_off";
    if (callCameraLabel) callCameraLabel.textContent = enabled ? "Camera off" : "Camera on";
    if (callSelfTile) callSelfTile.classList.toggle("call-tile-off", !enabled);
  }

  // Video is the first thing a poor link cannot carry, and the outbound ceiling is the lever
  // that costs the least quality: each sample that reports loss or a shrinking outgoing
  // estimate halves the video budget, and only when that budget is already at the floor does
  // the camera go off. Audio is never reduced.
  function startStatsMonitor() {
    window.clearInterval(call.statsTimer);
    call.statsTimer = null;
    if (call.media !== "video") return;
    call.statsTimer = window.setInterval(async () => {
      if (call.state !== "in-call") return;
      let lossy = false;
      let outgoing = Infinity;
      for (const peerState of call.peers.values()) {
        try {
          const report = await peerState.connection.getStats();
          report.forEach((stat) => {
            if (
              stat.type === "inbound-rtp" &&
              stat.kind === "video" &&
              (stat.fractionLost || 0) > 0.15
            ) {
              lossy = true;
            }
            if (
              stat.type === "candidate-pair" &&
              stat.state === "succeeded" &&
              stat.availableOutgoingBitrate > 0
            ) {
              outgoing = Math.min(outgoing, stat.availableOutgoingBitrate);
            }
          });
        } catch {
          // A closed peer connection has no stats to read.
        }
      }
      // Only a call that is actually sending video has a budget worth lowering: with the
      // camera off the outgoing estimate says nothing about what a future stream could use.
      const congested = lossy || (call.cameraOn && outgoing < call.videoMaxBitrate / 2);
      call.degradedSamples = congested ? call.degradedSamples + 1 : 0;
      if (call.degradedSamples < 2) return;
      call.degradedSamples = 0;
      if (call.videoMaxBitrate > VIDEO_MIN_BITRATE) {
        call.videoMaxBitrate = Math.max(
          VIDEO_MIN_BITRATE,
          Math.floor(call.videoMaxBitrate / 2)
        );
        call.videoDegraded = true;
        capAllSenders();
        if (callDetail) {
          callDetail.textContent = "Poor connection: lower video quality, audio continues.";
          callDetail.hidden = false;
        }
      } else if (call.cameraOn) {
        setCameraEnabled(false);
        if (callDetail) {
          callDetail.textContent = "Poor connection: camera off, audio continues.";
          callDetail.hidden = false;
        }
      }
    }, 5000);
  }

  function closeCallMedia() {
    window.clearInterval(call.timer);
    window.clearInterval(call.statsTimer);
    call.timer = null;
    call.statsTimer = null;
    for (const userId of Array.from(call.peers.keys())) closePeer(userId);
    call.localStream?.getTracks().forEach((track) => track.stop());
    call.localStream = null;
    if (callSelfVideo) callSelfVideo.srcObject = null;
    if (callRemotes) {
      callRemotes.replaceChildren();
      callRemotes.hidden = true;
    }
    if (callSelfTile) callSelfTile.hidden = true;
    if (callSafety) callSafety.hidden = true;
    if (callSafetyNote) callSafetyNote.textContent = "";
    if (callSafetyList) {
      callSafetyList.hidden = true;
      callSafetyList.replaceChildren();
    }
    call.muted = false;
    call.cameraOn = false;
    call.degradedSamples = 0;
    call.videoMaxBitrate = VIDEO_BITRATE_BY_PEERS[1];
    call.videoDegraded = false;
    if (callMute) callMute.setAttribute("aria-pressed", "false");
    if (callMuteIcon) callMuteIcon.textContent = "mic";
    setCameraEnabled(false);
  }

  // Returns to idle and reports the outcome. A terminal message lingers long enough to read.
  function finishCall(status, { hideAfter = 8000 } = {}) {
    closeCallMedia();
    call.id = null;
    call.payload = null;
    call.state = "idle";
    if (!callPanel) return;
    callPanel.hidden = !status;
    callStatus.textContent = status || "";
    if (callDetail) {
      callDetail.textContent = "";
      callDetail.hidden = true;
    }
    setCallButtons({});
    if (status && hideAfter) {
      call.hideTimer = window.setTimeout(() => {
        callPanel.hidden = true;
      }, hideAfter);
    }
  }

  async function endActiveCall(reason = "hangup") {
    if (!call.id) return;
    const callId = call.id;
    const group = call.payload?.kind === "group";
    const others = otherParticipants(call.payload, "joined").length;
    const leaving = group && others > 0;
    const path = leaving ? `/calls/${callId}/leave` : `/calls/${callId}/end`;
    const init = {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
    };
    if (!leaving) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify({ reason });
    }
    try {
      await fetch(path, init);
    } catch {
      // The socket may already be gone; the local teardown still applies.
    }
  }

  async function startCall(media) {
    if (call.state !== "idle") return;
    if (socket?.readyState !== WebSocket.OPEN) {
      finishCall("The conversation is not connected yet. Try again in a moment.");
      return;
    }
    call.state = "starting";
    call.media = media;
    call.muted = false;
    call.cameraOn = false;
    showPanel("Requesting your microphone…", "", { hangup: true, hangupLabel: "Cancel" });
    let stream;
    try {
      stream = await openLocalMedia(media);
    } catch (error) {
      finishCall(error.message, { hideAfter: 10000 });
      return;
    }
    if (call.state !== "starting") {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    call.localStream = stream;
    call.iceServers = await loadIceServers();
    try {
      const payload = await callRequest(`/conversations/${conversationId}/calls`, {
        body: JSON.stringify({ media }),
      });
      enterCall(payload);
    } catch (error) {
      finishCall(error.message, { hideAfter: 10000 });
    }
  }

  async function acceptCall() {
    if (!call.payload || call.state !== "ringing" || !call.id) return;
    const callId = call.id;
    const media = call.payload.media;
    call.state = "joining";
    showPanel("Connecting…", "", { hangup: true, hangupLabel: "Cancel" });
    let stream;
    try {
      stream = await openLocalMedia(media);
    } catch (error) {
      call.id = callId;
      endActiveCall("hangup");
      finishCall(error.message, { hideAfter: 10000 });
      return;
    }
    if (call.state !== "joining" || call.id !== callId) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    call.localStream = stream;
    call.iceServers = await loadIceServers();
    try {
      const payload = await callRequest(`/calls/${callId}/accept`);
      enterCall(payload);
    } catch (error) {
      finishCall(error.message, { hideAfter: 10000 });
    }
  }

  async function declineInvite() {
    if (!call.id) return;
    const callId = call.id;
    try {
      await callRequest(`/calls/${callId}/decline`);
    } catch {
      // The ring may already be over; the local teardown below still applies.
    }
  }

  if (typeof RTCPeerConnection === "undefined") {
    [startAudioCall, startVideoCall].forEach((button) => {
      if (!button) return;
      button.disabled = true;
      button.title = "Calls are not supported by this browser";
    });
  }
  startAudioCall?.addEventListener("click", () => startCall("audio"));
  startVideoCall?.addEventListener("click", () => startCall("video"));
  callAnswer?.addEventListener("click", acceptCall);
  callDecline?.addEventListener("click", async () => {
    await declineInvite();
    finishCall("Call declined.");
  });
  callMute?.addEventListener("click", () => setMuted(!call.muted));
  callCamera?.addEventListener("click", () => setCameraEnabled(!call.cameraOn));
  callHangup?.addEventListener("click", async () => {
    if (!call.id) {
      finishCall("Call cancelled.");
      return;
    }
    const group = call.payload?.kind === "group";
    const others = otherParticipants(call.payload, "joined").length;
    const message = group && others ? "You left the call." : "Call ended.";
    await endActiveCall("hangup");
    finishCall(message);
  });

  // Leaving the page is a hangup or a departure; keepalive gives the request a chance even as
  // the document unloads. The server also drops a participant whose socket closed.
  window.addEventListener("pagehide", () => {
    if (!call.id) return;
    const group = call.payload?.kind === "group";
    const others = otherParticipants(call.payload, "joined").length;
    const path =
      group && others ? `/calls/${call.id}/leave` : `/calls/${call.id}/end`;
    fetch(path, {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: path.endsWith("/end") ? JSON.stringify({ reason: "hangup" }) : undefined,
    });
  });

  if (messageForm) {
    attachmentInput?.addEventListener("change", refreshAttachmentSummary);
    // One key per logical message: a retry after a lost response replays the stored outcome
    // instead of posting the message twice, while editing the text produces a different key
    // because the request content changed.
    let attemptKey = null;
    let attemptBody = null;
    const idempotencyKeyFor = (body) => {
      if (attemptBody !== body) {
        attemptBody = body;
        attemptKey = newIdempotencyKey();
      }
      return attemptKey;
    };

    messageForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const bodyField = messageForm.elements.namedItem("body");
      const sendButton = messageForm.querySelector("button[type=submit]");
      const picked = attachmentInput ? Array.from(attachmentInput.files || []) : [];
      if ((!bodyField.value.trim() && picked.length === 0) || sendButton.disabled) return;
      sendButton.disabled = true;
      messageError.textContent = "";
      try {
        // Re-encoding before the upload is the only step that saves bytes on every later
        // read as well as this send.
        const shrink = globalThis.SocialMediaOptimize?.shrinkImage;
        const files = shrink ? await Promise.all(picked.map((file) => shrink(file))) : picked;
        const progressFor = (index) => (fraction) => {
          attachmentProgress.textContent =
            `Uploading ${index + 1}/${files.length}: ${Math.round(fraction * 100)}%`;
        };
        const attachmentIds = [];
        if (files.length) {
          // The first target reports the per-message ceiling, so an over-full message is
          // refused before any further bytes are sent.
          const first = await requestUploadTarget(files[0], await checksumFor(files[0]));
          if (files.length > first.max_attachments) {
            throw new Error(`A message can carry ${first.max_attachments} attachments.`);
          }
          attachmentIds.push((await completeUpload(first, files[0], progressFor(0))).id);
          for (let index = 1; index < files.length; index += 1) {
            const target = await requestUploadTarget(
              files[index],
              await checksumFor(files[index])
            );
            attachmentIds.push((await completeUpload(target, files[index], progressFor(index))).id);
          }
        }
        const data = new FormData(messageForm);
        // The file input is client-side only: the bytes already went to storage, and the
        // message references them by id.
        data.delete("attachments");
        data.set("idempotency_key", idempotencyKeyFor(bodyField.value));
        attachmentIds.forEach((id) => data.append("attachment_ids", id));
        const response = await fetch(messageForm.action, {
          method: "POST",
          body: data,
          headers: { "X-Chat-Request": "true" },
        });
        if (!response.ok) {
          const error = await response.json();
          throw new Error(errorMessage(error, "Message could not be sent."));
        }
        appendMessage(await response.json());
        // The reader just sent it, so it belongs in view whatever they were looking at.
        scrollToLatest();
        attemptKey = null;
        attemptBody = null;
        bodyField.value = "";
        if (attachmentInput) attachmentInput.value = "";
        bodyField.focus();
      } catch (error) {
        messageError.textContent = error.message;
      } finally {
        refreshAttachmentSummary();
        sendButton.disabled = false;
      }
    });
  }

  // The list grows with its content on narrow layouts, so the window scrolls instead of the
  // list; scrolling a row into view works for either case.
  function scrollToLatest() {
    const last = messageList.lastElementChild;
    if (last) last.scrollIntoView({ block: "end", inline: "nearest" });
    messageList.scrollTop = messageList.scrollHeight;
  }

  // True when the newest message is already in view, so a message arriving now should follow
  // it. Measured through the visible edge, because on narrow layouts the document scrolls and
  // the list's own scroll metrics are meaningless.
  function isNearLatest() {
    const last = messageList.lastElementChild;
    if (!last) return true;
    const listBottom = messageList.getBoundingClientRect().bottom;
    const visibleBottom = Math.min(window.innerHeight, listBottom);
    return last.getBoundingClientRect().bottom <= visibleBottom + 120;
  }

  // Where the pane should sit, kept until the reader scrolls: images and attachments change
  // their own height after they load, which would otherwise push the target out of view.
  let readingTarget = null;
  let readingBlock = "start";
  let readingUntil = 0;

  function holdReadingPosition() {
    if (Date.now() > readingUntil || !readingTarget?.isConnected) return;
    readingTarget.scrollIntoView({ block: readingBlock, inline: "nearest" });
  }

  ["wheel", "touchstart", "keydown"].forEach((name) =>
    window.addEventListener(name, () => {
      readingUntil = 0;
    }, { passive: true })
  );

  // Open where reading resumes: the first unread message, or the newest when nothing is new.
  // After "Load earlier messages", the boundary message is the anchor and stays at the bottom
  // of the pane, so the just-loaded history is what the reader sees.
  function scrollToReadingStart() {
    const anchorId = view.dataset.anchorId;
    const anchor = anchorId && messageList.querySelector(`[data-message-id="${anchorId}"]`);
    if (anchor) {
      readingTarget = anchor;
      readingBlock = "end";
    } else {
      readingTarget = messageList.querySelector('[data-first-unread="true"]');
      readingBlock = "start";
    }
    if (!readingTarget) {
      readingTarget = messageList.lastElementChild;
      readingBlock = "end";
    }
    readingUntil = Date.now() + 8000;
    holdReadingPosition();
  }

  // Server-rendered attachments: preview only what is visible, and fetch the rest when the
  // reader clicks it. An expired row restores from this browser's copy if there is one.
  messageList.querySelectorAll("[data-attachment-id]").forEach((element) => {
    if (element.dataset.expired === "true") {
      prepareAttachment(element);
      return;
    }
    if (
      element.dataset.attachmentInline === "true" &&
      element.dataset.attachmentKind === "image"
    ) {
      prepareWhenVisible(element);
      interceptDownload(element, { open: true });
      return;
    }
    interceptDownload(element);
  });

  scrollToReadingStart();
  window.addEventListener("load", scrollToReadingStart, { once: true });
  connect();
  window.setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
  }, 25000);
})();
