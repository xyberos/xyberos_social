(() => {
  let timer;
  let requestNumber = 0;

  function bind() {
    const form = document.querySelector(".people-search");
    const input = form?.querySelector('input[name="search"]');
    if (!form || !input || !document.querySelector("#people-results") || form.dataset.liveSearchBound === "true") return;
    form.dataset.liveSearchBound = "true";

    async function updateResults(pushHistory = false) {
      const current = ++requestNumber;
      const url = new URL(form.action, window.location.origin);
      const term = input.value.trim();
      if (term) url.searchParams.set("search", term);
      document.querySelector("#people-results")?.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(url, {
          headers: { "HX-Request": "true", "X-Requested-With": "fetch" },
          credentials: "same-origin",
        });
        if (!response.ok) return;
        const markup = await response.text();
        if (current !== requestNumber) return;
        const documentFromResponse = new DOMParser().parseFromString(markup, "text/html");
        const replacement = documentFromResponse.querySelector("#people-results");
        if (!replacement) return;
        document.querySelector("#people-results")?.replaceWith(replacement);
        if (pushHistory) {
          const historyUrl = term ? `${url.pathname}?search=${encodeURIComponent(term)}` : url.pathname;
          window.history.replaceState({}, "", historyUrl);
        }
        bind();
      } catch {
        // Keep the current results available if the connection is temporarily unavailable.
      } finally {
        if (current === requestNumber) {
          document.querySelector("#people-results")?.setAttribute("aria-busy", "false");
        }
      }
    }

    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => updateResults(true), 220);
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      window.clearTimeout(timer);
      updateResults(true);
    });
  }

  document.addEventListener("DOMContentLoaded", bind);
  document.addEventListener("htmx:afterSwap", bind);
  document.addEventListener("htmx:historyRestore", bind);
})();
