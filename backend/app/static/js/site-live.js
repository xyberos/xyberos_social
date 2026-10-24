(() => {
  let revision;
  let pendingRefresh = false;
  let checking = false;

  function hasUnsavedFormChanges() {
    return Array.from(document.querySelectorAll("form")).some((form) => {
      return Array.from(form.elements).some((field) => {
        if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement || field instanceof HTMLSelectElement)) return false;
        if (field.type === "hidden" || field.type === "submit" || field.type === "button") return false;
        if (field instanceof HTMLInputElement && ["checkbox", "radio"].includes(field.type)) return field.checked !== field.defaultChecked;
        if (field instanceof HTMLSelectElement) return Array.from(field.options).some((option) => option.selected !== option.defaultSelected);
        return field.value !== field.defaultValue;
      });
    });
  }

  async function poll() {
    if (checking || document.hidden) return;
    checking = true;
    try {
      const response = await fetch("/api/live/revision", {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401) return;
      if (!response.ok) return;
      const payload = await response.json();
      if (revision === undefined) {
        revision = payload.revision;
      } else if (payload.revision !== revision) {
        revision = payload.revision;
        pendingRefresh = true;
      }
      if (pendingRefresh && !hasUnsavedFormChanges()) window.location.reload();
    } catch {
      // A missed poll is recovered on the next pass; no page data is carried in this channel.
    } finally {
      checking = false;
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });
  window.setInterval(poll, 5000);
  poll();
})();
