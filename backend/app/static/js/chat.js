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
    const record = await cacheAttachment(element);
    if (record) {
      if (element.dataset.expired === "true") hydrateFromCache(element, record);
      else showFromCache(element, record);
    } else if (element.dataset.expired !== "true" && element.dataset.attachmentInline === "true") {
      // No local copy: a live attachment still has bytes on the server, an expired one does not.
      const image = element.querySelector("img[data-attachment-image]");
      if (image) image.src = element.dataset.attachmentSrc;
    }
    // A rendered image is taller than the placeholder, so the reading position is re-applied.
    holdReadingPosition();
  }

  function interceptDownload(element) {
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
      link.download = record.filename;
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
      element.dataset.attachmentSize = String(attachment.size_bytes);
      if (attachment.expired) {
        element.dataset.expired = "true";
        element.textContent = `${attachment.filename} · Attachment expired`;
      } else if (attachment.inline) {
        element.href = attachment.download_url;
        element.target = "_blank";
        element.rel = "noopener";
        const image = document.createElement("img");
        image.dataset.attachmentImage = "";
        image.alt = attachment.filename;
        image.loading = "lazy";
        element.append(image);
      } else {
        element.href = attachment.download_url;
        element.textContent = `${attachment.filename} · ${Math.round(attachment.size_bytes / 1024)} KB`;
        interceptDownload(element);
      }
      container.append(element);
      prepareAttachment(element);
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
      throw new Error(errorMessage(error, "That file was refused."));
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
      throw new Error(errorMessage(error, "The upload was rejected."));
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
      const files = attachmentInput ? Array.from(attachmentInput.files || []) : [];
      if ((!bodyField.value.trim() && files.length === 0) || sendButton.disabled) return;
      sendButton.disabled = true;
      messageError.textContent = "";
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

  // Server-rendered attachments: fetch each once, keep it locally, and restore an expired one
  // from this browser's copy.
  messageList.querySelectorAll("[data-attachment-id]").forEach((element) => {
    if (element.dataset.expired !== "true" && element.dataset.attachmentInline !== "true") {
      interceptDownload(element);
    }
    prepareAttachment(element);
  });

  scrollToReadingStart();
  window.addEventListener("load", scrollToReadingStart, { once: true });
  connect();
  window.setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
  }, 25000);
})();
