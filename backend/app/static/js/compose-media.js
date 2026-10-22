// The composer's media picker.
//
// Bytes never travel through the application: the page asks for a short-lived target, posts
// the file straight to object storage, and only then tells the server to verify and scan it.
// The form carries the resulting ids as repeated `attachment_ids` fields, so publishing stays
// a plain form post and the server decides what each id really is.
//
// Everything here is enhancement. Without this file the picker uploads nothing and the hint
// beside it says so, which is why the field is not `required` and the form still submits.
(() => {
  const picker = document.querySelector("[data-media-picker]");
  if (!picker) return;

  const form = picker.closest("form");
  const input = picker.querySelector("[data-media-input]");
  const list = picker.querySelector("[data-media-list]");
  const status = picker.querySelector("[data-media-status]");
  if (!form || !input || !list || !status) return;

  const maxItems = Number(picker.dataset.mediaMaxItems) || 4;
  const limits = {
    image: Number(picker.dataset.mediaMaxImage) || 0,
    video: Number(picker.dataset.mediaMaxVideo) || 0,
    audio: Number(picker.dataset.mediaMaxAudio) || 0,
  };
  const KINDS = { image: "Photo", video: "Video", audio: "Audio" };
  const GLYPHS = { image: "image", video: "movie", audio: "graphic_eq" };

  const tiles = new Map();
  const pending = new Set();

  const megabytes = (bytes) => Math.max(1, Math.round(bytes / (1024 * 1024)));

  function kindOf(file) {
    const type = (file.type || "").toLowerCase();
    if (type.startsWith("image/")) return "image";
    if (type.startsWith("video/")) return "video";
    if (type.startsWith("audio/")) return "audio";
    const extension = (file.name.split(".").pop() || "").toLowerCase();
    if (["jpg", "jpeg", "png", "gif", "webp"].includes(extension)) return "image";
    if (["mp4", "webm"].includes(extension)) return "video";
    if (["mp3", "m4a", "ogg", "wav"].includes(extension)) return "audio";
    return null;
  }

  function setStatus(message) {
    status.textContent = message;
  }

  function readyStatus() {
    const count = tiles.size;
    setStatus(
      count
        ? `${count} of ${maxItems} file${count === 1 ? "" : "s"} ready to publish.`
        : "",
    );
  }

  function newIdempotencyKey() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return `upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function requestTarget(file) {
    const response = await fetch("/posts/media", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": newIdempotencyKey() },
      credentials: "same-origin",
      body: JSON.stringify({ filename: file.name, content_type: file.type, size_bytes: file.size }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "That file was refused.");
    }
    return response.json();
  }

  function sendToStorage(target, file, onProgress) {
    return new Promise((resolve, reject) => {
      const body = new FormData();
      Object.entries(target.fields).forEach(([name, value]) => body.append(name, value));
      body.append("file", file);
      const upload = new XMLHttpRequest();
      upload.open("POST", target.upload_url);
      upload.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      });
      upload.addEventListener("load", () => {
        if (upload.status >= 200 && upload.status < 300) resolve();
        else reject(new Error("The upload did not reach storage."));
      });
      upload.addEventListener("error", () => reject(new Error("The upload failed.")));
      upload.send(body);
    });
  }

  async function finalize(attachmentId) {
    const response = await fetch(`/attachments/${attachmentId}/finalize`, {
      method: "POST",
      credentials: "same-origin",
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "The upload was rejected.");
    }
    return response.json();
  }

  function tileFor(file, kind) {
    const item = document.createElement("li");
    item.className = "compose-media-item";

    const preview = document.createElement("span");
    preview.className = "compose-media-preview";
    let objectUrl = null;
    if (kind === "image") {
      objectUrl = URL.createObjectURL(file);
      const image = document.createElement("img");
      image.src = objectUrl;
      image.alt = "";
      preview.append(image);
    } else {
      const glyph = document.createElement("span");
      glyph.className = "material-symbols-rounded";
      glyph.setAttribute("aria-hidden", "true");
      glyph.textContent = GLYPHS[kind];
      preview.append(glyph);
    }

    const meta = document.createElement("span");
    meta.className = "compose-media-meta";
    const name = document.createElement("span");
    name.className = "compose-media-name";
    name.textContent = file.name;
    const detail = document.createElement("span");
    detail.className = "compose-media-detail";
    detail.textContent = `${KINDS[kind]} · ${megabytes(file.size)} MB`;
    meta.append(name, detail);

    const bar = document.createElement("span");
    bar.className = "compose-media-progress";
    const fill = document.createElement("span");
    fill.style.width = "0%";
    bar.append(fill);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "compose-media-remove";
    remove.setAttribute("aria-label", `Remove ${file.name}`);
    remove.innerHTML = '<span class="material-symbols-rounded" aria-hidden="true">close</span>';

    item.append(preview, meta, bar, remove);
    const entry = { item, fill, detail, remove, objectUrl, attachmentId: null, cancelled: false };
    remove.addEventListener("click", () => discard(entry));
    list.append(item);
    return entry;
  }

  async function discard(entry) {
    entry.cancelled = true;
    entry.item.remove();
    if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
    tiles.delete(entry.item);
    if (entry.field) entry.field.remove();
    if (entry.attachmentId) {
      // The bytes are already in storage, so the release is a request, not a hope: a draft
      // dropped here should not sit in the account's quota until it would expire.
      await fetch(`/posts/media/${entry.attachmentId}`, {
        method: "DELETE",
        credentials: "same-origin",
      }).catch(() => {});
    }
    readyStatus();
  }

  function track(entry, file, kind) {
    pending.add(entry);
    const work = (async () => {
      const target = await requestTarget(file);
      if (entry.cancelled) return;
      entry.attachmentId = target.attachment_id;
      const ceiling = limits[kind];
      if (ceiling && file.size > ceiling) {
        throw new Error(
          `${KINDS[kind]} files are limited to ${megabytes(ceiling)} MB.`,
        );
      }
      await sendToStorage(target, file, (fraction) => {
        entry.fill.style.width = `${Math.round(fraction * 100)}%`;
        entry.detail.textContent = `${KINDS[kind]} · uploading ${Math.round(fraction * 100)}%`;
      });
      if (entry.cancelled) return;
      entry.detail.textContent = `${KINDS[kind]} · checking`;
      const verified = await finalize(target.attachment_id);
      if (entry.cancelled) return;
      entry.fill.style.width = "100%";
      entry.fill.classList.add("is-done");
      entry.detail.textContent = `${KINDS[kind]} · ${megabytes(verified.size_bytes)} MB · ready`;
      // The id the post will carry. A disabled field is not submitted, so removing the tile
      // removes the field and the post never learns the file existed.
      const field = document.createElement("input");
      field.type = "hidden";
      field.name = "attachment_ids";
      field.value = verified.id;
      form.append(field);
      entry.field = field;
    })()
      .catch((error) => {
        entry.fill.classList.add("is-failed");
        entry.detail.textContent = error.message;
        if (entry.attachmentId) {
          fetch(`/posts/media/${entry.attachmentId}`, { method: "DELETE" }).catch(() => {});
          entry.attachmentId = null;
        }
        setStatus(error.message);
      })
      .finally(() => {
        pending.delete(entry);
        if (!status.textContent || status.textContent.startsWith("Waiting")) readyStatus();
      });
    entry.done = work;
    return work;
  }

  input.addEventListener("change", async () => {
    setStatus("");
    const files = Array.from(input.files || []);
    input.value = "";
    for (const file of files) {
      const kind = kindOf(file);
      if (!kind) {
        setStatus(`"${file.name}" is not a photo, video, or audio file this composer accepts.`);
        continue;
      }
      if (tiles.size >= maxItems) {
        setStatus(`A post can carry ${maxItems} files. Remove one to add another.`);
        break;
      }
      const ceiling = limits[kind];
      if (ceiling && file.size > ceiling) {
        setStatus(
          `${KINDS[kind]} files are limited to ${megabytes(ceiling)} MB, so "${file.name}" is too large.`,
        );
        continue;
      }
      const entry = tileFor(file, kind);
      tiles.set(entry.item, entry);
      track(entry, file, kind);
    }
  });

  // A submit while files are still in flight would publish a post without them, so the form
  // waits for the uploads and then sends itself again.
  form.addEventListener("submit", async (event) => {
    if (!pending.size) return;
    event.preventDefault();
    setStatus("Waiting for your files to finish uploading…");
    await Promise.allSettled(Array.from(pending).map((entry) => entry.done));
    form.requestSubmit();
  });
})();
