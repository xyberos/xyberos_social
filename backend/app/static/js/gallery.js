// The photo viewer for a post's gallery.
//
// A tile is an ordinary link to the authorized download route, so it opens the original when
// this file is missing or fails. When it is here, a click opens that same original in a modal
// `<dialog>`: the element gives the focus trap, the Escape key, and focus restoration for free,
// which is the part of a lightbox that is usually wrong.
//
// Everything is read from the tile itself (`data-full`, and the preview's alt text), so the
// server renders no second copy of the image metadata.
(() => {
  let viewer = null;
  // The tiles of the gallery the reader opened, and which of them is on screen.
  let items = [];
  let position = 0;
  let opener = null;

  function glyph(name) {
    const icon = document.createElement("span");
    icon.className = "material-symbols-rounded";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = name;
    return icon;
  }

  function button(className, label, icon) {
    const element = document.createElement("button");
    element.type = "button";
    element.className = className;
    element.setAttribute("aria-label", label);
    element.append(glyph(icon));
    return element;
  }

  function build() {
    const dialog = document.createElement("dialog");
    dialog.className = "media-viewer";
    dialog.setAttribute("aria-label", "Photo viewer");

    const figure = document.createElement("figure");
    figure.className = "media-viewer-figure";
    const image = document.createElement("img");
    image.className = "media-viewer-image";
    image.decoding = "async";
    const caption = document.createElement("figcaption");
    caption.className = "media-viewer-caption";
    figure.append(image, caption);

    const close = button("media-viewer-close", "Close photo viewer", "close");
    const previous = button("media-viewer-prev", "Previous photo", "chevron_left");
    const next = button("media-viewer-next", "Next photo", "chevron_right");
    dialog.append(figure, close, previous, next);
    document.body.append(dialog);

    close.addEventListener("click", () => closeViewer());
    previous.addEventListener("click", () => step(-1));
    next.addEventListener("click", () => step(1));
    // A modal dialog covers the viewport, so a click that lands on the dialog itself is a
    // click on the backdrop.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeViewer();
    });
    // Fires for Escape as well as for our own close(), which is the one place to clean up.
    dialog.addEventListener("close", () => {
      image.removeAttribute("src");
      if (opener?.isConnected) opener.focus();
    });

    viewer = { dialog, image, caption, previous, next };
  }

  function render() {
    const tile = items[position];
    const preview = tile.querySelector("img");
    viewer.image.src = tile.dataset.full || tile.getAttribute("href");
    viewer.image.alt = preview?.alt || "";
    viewer.caption.textContent = preview?.alt || "";
    const single = items.length < 2;
    viewer.previous.hidden = single;
    viewer.next.hidden = single;
  }

  function openViewer(gallery, tile) {
    if (!viewer) build();
    items = Array.from(gallery.querySelectorAll("[data-gallery-item]"));
    position = Math.max(0, items.indexOf(tile));
    opener = tile;
    render();
    if (typeof viewer.dialog.showModal === "function") viewer.dialog.showModal();
    else viewer.dialog.setAttribute("open", "");
    viewer.dialog.querySelector(".media-viewer-close").focus();
  }

  function closeViewer() {
    if (!viewer) return;
    if (typeof viewer.dialog.close === "function") viewer.dialog.close();
    else viewer.dialog.removeAttribute("open");
  }

  function step(delta) {
    if (!viewer || items.length < 2) return;
    position = (position + delta + items.length) % items.length;
    render();
  }

  document.addEventListener("click", (event) => {
    const tile = event.target.closest?.("[data-gallery-item]");
    if (!tile) return;
    // Leave modified clicks alone: they mean "open the original in a new tab".
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    const gallery = tile.closest("[data-gallery]");
    if (!gallery) return;
    event.preventDefault();
    openViewer(gallery, tile);
  });

  document.addEventListener("keydown", (event) => {
    if (!viewer?.dialog.hasAttribute("open")) return;
    if (event.key === "Escape") {
      // Handled explicitly rather than relying on the platform's cancel behaviour, which is
      // not uniform across browsers and is swallowed when focus sits on a control.
      event.preventDefault();
      closeViewer();
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      step(1);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      step(-1);
    }
  });
})();
