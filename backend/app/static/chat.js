(() => {
  const view = document.querySelector("[data-conversation-id]");
  if (!view) return;

  const conversationId = view.dataset.conversationId;
  const currentUserId = view.dataset.currentUserId;
  const messageList = document.querySelector("#message-list");
  const messageForm = document.querySelector("#message-form");
  const messageError = document.querySelector("#message-error");
  const connectionState = document.querySelector("#chat-connection-state");
  let socket;
  let reconnectDelay = 1000;

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
    row.append(bubble, time);
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
    // One key per logical message: a retry after a lost response replays the stored
    // outcome instead of posting the message twice, while editing the text produces a
    // different key because the request content changed. The fallback covers origins
    // where crypto.randomUUID is unavailable because the page is not a secure context.
    const newIdempotencyKey = () => {
      if (window.crypto?.randomUUID) return window.crypto.randomUUID();
      return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
    };
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
      const data = new FormData(messageForm);
      data.set("idempotency_key", idempotencyKeyFor(bodyField.value));
      try {
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
        bodyField.focus();
      } catch (error) {
        messageError.textContent = error.message;
      } finally {
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
