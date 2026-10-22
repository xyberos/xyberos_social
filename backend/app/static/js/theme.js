// Theme switching.
//
// The server already rendered the right palette, so this script never decides the first paint:
// it upgrades the menu from "post the form and reload" to "switch now and remember in the
// background", and previews the radio group on the settings page. If JavaScript is missing,
// every option is still a working submit button.
(() => {
  const root = document.documentElement;
  const menu = document.querySelector(".theme-menu");
  const options = menu ? Array.from(menu.querySelectorAll(".theme-option")) : [];
  const trigger = menu ? menu.querySelector(".theme-trigger") : null;

  const ICONS = { system: "brightness_auto", light: "light_mode", dark: "dark_mode" };
  const LABELS = { system: "System", light: "Light", dark: "Dark" };

  function apply(choice) {
    if (choice === "system") root.removeAttribute("data-theme");
    else root.dataset.theme = choice;
    root.dataset.themeChoice = choice;
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.setAttribute("content", choice === "system" ? "light dark" : choice);
    options.forEach((option) => {
      option.setAttribute(
        "aria-pressed",
        option.dataset.themeValue === choice ? "true" : "false"
      );
    });
    if (trigger) {
      const label = LABELS[choice] || LABELS.system;
      trigger.setAttribute("aria-label", `Theme: ${label}`);
      trigger.title = `Theme: ${label}`;
      const icon = trigger.querySelector(".material-symbols-rounded");
      if (icon) icon.textContent = ICONS[choice] || ICONS.system;
    }
  }

  if (menu) {
    // The options are submit buttons so the control works without JavaScript. With it, the
    // request below is the only one that should happen, so a stray submit never navigates.
    const form = menu.querySelector("form");
    form?.addEventListener("submit", (event) => event.preventDefault());

    menu.addEventListener("click", async (event) => {
      const option = event.target.closest(".theme-option");
      if (!option) return;
      event.preventDefault();
      const choice = option.dataset.themeValue;
      menu.open = false;
      apply(choice);
      try {
        await fetch("/theme", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Theme-Update": "true",
          },
          body: new URLSearchParams({ theme: choice }),
        });
      } catch {
        // The page keeps the new look for this visit; the next request shows the saved choice.
      }
    });

    // A native disclosure stays open until it is told otherwise; close it on an outside click.
    document.addEventListener("click", (event) => {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && menu.open) {
        menu.open = false;
        trigger?.focus();
      }
    });
  }

  // The settings page offers the same three choices as radios; preview before saving.
  document.querySelectorAll('input[name="theme"][data-preview]').forEach((input) => {
    input.addEventListener("change", () => apply(input.value));
  });
})();
