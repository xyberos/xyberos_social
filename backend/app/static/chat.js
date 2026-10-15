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

  function appendMessage(message) {
    if (messageList.querySelector(`[data-message-id="${message.id}"]`)) return;
    const wasNearBottom = messageList.scrollHeight - messageList.scrollTop - messageList.clientHeight < 120;
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
    const bubble = document.createElement("p");
    bubble.className = "message-bubble";
    bubble.textContent = message.body;
    const time = document.createElement("time");
    time.dateTime = message.created_at;
    time.textContent = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(message.created_at));
    row.append(bubble);
    renderAttachments(row, message.attachments);
    row.append(time);
    messageList.querySelector(".message-empty")?.remove();
    messageList.append(row);
    if (wasNearBottom) messageList.scrollTop = messageList.scrollHeight;
    if (message.sender_id !== currentUserId) markRead();
  }

  async function markRead() {
    const token = messageForm?.elements.namedItem("csrf_token")?.value;
    if (!token) return;
    const data = new FormData();
    data.set("csrf_token", token);
    await fetch(`/conversations/${conversationId}/read`, { method: "POST", body: data });
  }

  function renderAttachments(row, attachments) {
    if (!attachments?.length) return;
    const container = document.createElement("div");
    container.className = "message-attachments";
    attachments.forEach((attachment) => {
      const link = document.createElement("a");
      link.className = "message-attachment";
      link.href = attachment.download_url;
      if (attachment.inline) {
        link.target = "_blank";
        link.rel = "noopener";
        const image = document.createElement("img");
        image.src = attachment.download_url;
        image.alt = attachment.filename;
        image.loading = "lazy";
        link.append(image);
      } else {
        link.textContent = `${attachment.filename} · ${Math.round(attachment.size_bytes / 1024)} KB`;
      }
      container.append(link);
    });
    row.append(container);
  }

  async function requestUploadTarget(file) {
    const response = await fetch(`/conversations/${conversationId}/attachments`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": newIdempotencyKey() },
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type,
        size_bytes: file.size,
      }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "That file was refused.");
    }
    return response.json();
  }

  function sendToStorage(target, file, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      Object.entries(target.fields).forEach(([name, value]) => form.append(name, value));
      form.append("file", file);
      const upload = new XMLHttpRequest();
      upload.open("POST", target.upload_url);
      upload.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
      });
      upload.addEventListener("load", () => {
        if (upload.status >= 200 && upload.status < 300) resolve();
        else reject(new Error("The upload did not reach storage."));
      });
      upload.addEventListener("error", () => reject(new Error("The upload failed.")));
      upload.send(form);
    });
  }

  async function finalizeUpload(attachmentId) {
    const response = await fetch(`/attachments/${attachmentId}/finalize`, { method: "POST" });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "The upload was rejected.");
    }
    return response.json();
  }

  async function completeUpload(target, file, onProgress) {
    await sendToStorage(target, file, onProgress);
    return finalizeUpload(target.attachment_id);
  }

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${scheme}//${window.location.host}/ws/conversations/${conversationId}`);
    socket.addEventListener("open", () => {
      reconnectDelay = 1000;
      if (connectionState) connectionState.textContent = "Live";
    });
    socket.addEventListener("message", (event) => {
      if (event.data === "pong") return;
      try {
        const packet = JSON.parse(event.data);
        if (packet.type === "message") appendMessage(packet.message);
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
      window.setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    });
    socket.addEventListener("error", () => socket.close());
  }

  if (messageForm) {
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
      if (!bodyField.value.trim() || sendButton.disabled) return;
      sendButton.disabled = true;
      messageError.textContent = "";
      const files = attachmentInput ? Array.from(attachmentInput.files || []) : [];
      const progressFor = (index) => (fraction) => {
        attachmentProgress.textContent =
          `Uploading ${index + 1}/${files.length}: ${Math.round(fraction * 100)}%`;
      };
      try {
        const attachmentIds = [];
        if (files.length) {
          // The first target reports the per-message ceiling, so an over-full message is
          // refused before any further bytes are sent.
          const first = await requestUploadTarget(files[0]);
          if (files.length > first.max_attachments) {
            throw new Error(`A message can carry ${first.max_attachments} attachments.`);
          }
          attachmentIds.push((await completeUpload(first, files[0], progressFor(0))).id);
          for (let index = 1; index < files.length; index += 1) {
            const target = await requestUploadTarget(files[index]);
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
          throw new Error(error.detail || "Message could not be sent.");
        }
        appendMessage(await response.json());
        attemptKey = null;
        attemptBody = null;
        bodyField.value = "";
        if (attachmentInput) attachmentInput.value = "";
        bodyField.focus();
      } catch (error) {
        messageError.textContent = error.message;
      } finally {
        attachmentProgress.textContent = "";
        sendButton.disabled = false;
      }
    });
  }

  messageList.scrollTop = messageList.scrollHeight;
  connect();
  window.setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
  }, 25000);
})();
