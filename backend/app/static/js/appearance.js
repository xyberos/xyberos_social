// Preview account appearance choices immediately; the regular settings form persists them.
(() => {
  const root = document.documentElement;
  document.querySelectorAll("[data-appearance-preview] input").forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      const control = input.closest("[data-appearance-preview]");
      const attribute = control.dataset.appearancePreview;
      root.dataset[attribute] = input.value;
    });
  });
})();
