(() => {
  const badges = Array.from(document.querySelectorAll("[data-chat-unread]"));
  if (!badges.length) return;

  let socket;
  let attempts = 0;
  let reconnectTimer;

  function label(count) {
    return `${count} unread message${count === 1 ? "" : "s"}`;
  }

  function render(total) {
    const count = Number.isFinite(total) && total > 0 ? Math.trunc(total) : 0;
    badges.forEach((badge) => {
      badge.textContent = count ? String(count) : "";
      badge.hidden = count === 0;
      badge.setAttribute("aria-label", label(count));
    });
  }

  function requestRefresh() {
    if (socket?.readyState === WebSocket.OPEN) socket.send("refresh");
  }

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${scheme}//${window.location.host}/ws/inbox`);
    socket.addEventListener("open", () => {
      attempts = 0;
    });
    socket.addEventListener("message", (event) => {
      if (event.data === "pong") return;
      try {
        const packet = JSON.parse(event.data);
        if (packet.type === "chat_unread") render(packet.total);
      } catch {
        return;
      }
    });
    socket.addEventListener("close", () => {
      attempts += 1;
      const delay = Math.min(1000 * 2 ** (attempts - 1), 15000);
      window.clearTimeout(reconnectTimer);
      reconnectTimer = window.setTimeout(connect, delay);
    });
    socket.addEventListener("error", () => socket.close());
  }

  // Reading a conversation marks it read for every open tab, not just the one that opened it.
  document.addEventListener("chat:read", requestRefresh);
  // A backgrounded tab may have missed pushes while its socket was asleep.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) requestRefresh();
  });
  window.setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) socket.send("ping");
  }, 25000);

  connect();
})();
