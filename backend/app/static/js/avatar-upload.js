// Profile-photo editor: preview the chosen image in the circle before it is sent, and let the
// reader drop a file on the picker. This is an enhancement — without it the plain multipart
// form still chooses a file and uploads it.
(() => {
  const editor = document.querySelector("[data-avatar-editor]");
  if (!editor) return;

  const form = editor.querySelector("[data-avatar-form]");
  const input = editor.querySelector("[data-avatar-input]");
  const preview = editor.querySelector(".avatar-editor-image");
  const filename = editor.querySelector("[data-avatar-filename]");
  const submit = editor.querySelector("[data-avatar-submit]");
  const cancel = editor.querySelector("[data-avatar-cancel]");
  const picker = editor.querySelector(".avatar-picker");
  if (!form || !input || !preview || !submit) return;

  const original = preview.innerHTML;
  let previewUrl = null;

  function releasePreview() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  }

  function reset() {
    releasePreview();
    preview.innerHTML = original;
    if (filename) filename.textContent = "No photo selected";
    if (cancel) cancel.hidden = true;
    submit.disabled = true;
    picker?.classList.remove("is-dragging");
  }

  function show(file) {
    releasePreview();
    previewUrl = URL.createObjectURL(file);
    preview.replaceChildren();
    const image = document.createElement("img");
    image.src = previewUrl;
    image.alt = "";
    preview.append(image);
    if (filename) {
      filename.textContent = file.name;
    }
    if (cancel) cancel.hidden = false;
    submit.disabled = false;
  }

  function select(file) {
    if (!file) {
      reset();
      return;
    }
    if (!file.type.startsWith("image/")) {
      input.value = "";
      reset();
      if (filename) filename.textContent = "That file is not an image.";
      return;
    }
    show(file);
  }

  input.addEventListener("change", () => select(input.files?.[0]));

  cancel?.addEventListener("click", () => {
    input.value = "";
    reset();
    input.focus();
  });

  form.addEventListener("submit", (event) => {
    if (!input.files?.length) {
      event.preventDefault();
      input.focus();
      picker?.classList.add("is-attention");
      window.setTimeout(() => picker?.classList.remove("is-attention"), 900);
      return;
    }
    submit.disabled = true;
    submit.textContent = "Saving…";
  });

  if (picker) {
    for (const type of ["dragenter", "dragover"]) {
      picker.addEventListener(type, (event) => {
        event.preventDefault();
        picker.classList.add("is-dragging");
      });
    }
    for (const type of ["dragleave", "dragend"]) {
      picker.addEventListener(type, () => picker.classList.remove("is-dragging"));
    }
    picker.addEventListener("drop", (event) => {
      event.preventDefault();
      picker.classList.remove("is-dragging");
      const file = event.dataTransfer?.files?.[0];
      if (!file) return;
      try {
        const transfer = new DataTransfer();
        transfer.items.add(file);
        input.files = transfer.files;
      } catch {
        return;
      }
      select(file);
    });
  }

  // With JavaScript the Save button waits for a choice; the input itself remains the control.
  submit.disabled = true;
  if (input.files?.length) select(input.files[0]);
})();
