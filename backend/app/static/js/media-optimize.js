// Client-side upload shrinking.
//
// A phone photo is often several megabytes and several thousand pixels wide, but a message
// bubble or a feed tile never renders it larger than about a megapixel. Re-encoding before
// the upload is the only bit of this pipeline that saves bytes end to end: the same image
// costs less to send, less to store, and less to read back.
//
// Everything here is best effort. If the browser cannot decode, encode, or produce something
// smaller, the original file is returned unchanged and the server still owns the type, size,
// and scan checks.
(() => {
  const SHRINKABLE = new Set(["image/jpeg", "image/png", "image/webp"]);
  const MAX_EDGE = 1600;
  // Below this a re-encode is more likely to cost quality than it saves bandwidth.
  const MIN_BYTES = 512 * 1024;
  const QUALITY = 0.82;
  // A part that fails is retried before the whole upload is called a failure: the file's
  // other parts are already in storage, so restarting from zero would waste them.
  const PART_ATTEMPTS = 3;

  function canvasToBlob(canvas) {
    return new Promise((resolve) => canvas.toBlob(resolve, "image/webp", QUALITY));
  }

  function rename(file, extension) {
    const stem = file.name.replace(/\.[^./\\]+$/, "") || "image";
    return `${stem}.${extension}`;
  }

  async function shrinkImage(file) {
    if (!file || !SHRINKABLE.has((file.type || "").toLowerCase())) return file;
    if (file.size <= MIN_BYTES) return file;
    if (typeof createImageBitmap !== "function") return file;
    let bitmap;
    try {
      bitmap = await createImageBitmap(file);
    } catch {
      return file;
    }
    try {
      const edge = Math.max(bitmap.width, bitmap.height);
      const scale = edge > MAX_EDGE ? MAX_EDGE / edge : 1;
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const context = canvas.getContext("2d");
      if (!context) return file;
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const blob = await canvasToBlob(canvas);
      if (!blob || blob.size >= file.size) return file;
      return new File([blob], rename(file, "webp"), {
        type: "image/webp",
        lastModified: file.lastModified,
      });
    } catch {
      return file;
    } finally {
      bitmap.close?.();
    }
  }

  // The digest is what lets the server answer a repeat upload with a copy of bytes it already
  // holds. It is computed locally, so it costs CPU and no bandwidth, and it is best effort:
  // without a secure context the upload simply proceeds as an ordinary one.
  async function sha256(file) {
    if (!globalThis.crypto?.subtle || !file?.arrayBuffer) return null;
    try {
      const digest = await globalThis.crypto.subtle.digest(
        "SHA-256",
        await file.arrayBuffer()
      );
      return Array.from(new Uint8Array(digest), (byte) =>
        byte.toString(16).padStart(2, "0")
      ).join("");
    } catch {
      return null;
    }
  }

  function sleep(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
  }

  function putPart(url, blob, attempt = 1) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("PUT", url);
      request.addEventListener("load", () => {
        const etag = request.getResponseHeader("ETag");
        if (request.status >= 200 && request.status < 300 && etag) {
          resolve(etag);
          return;
        }
        reject(new Error("A part of the upload did not reach storage."));
      });
      request.addEventListener("error", () =>
        reject(new Error("A part of the upload did not reach storage."))
      );
      request.send(blob);
    }).catch(async (error) => {
      if (attempt >= PART_ATTEMPTS) throw error;
      await sleep(500 * 2 ** (attempt - 1));
      return putPart(url, blob, attempt + 1);
    });
  }

  // A part session is started, and resumed, by the server: asking twice returns the same
  // upload id, so the parts already delivered are still accounted for.
  async function uploadMultipart(attachmentId, file, onProgress) {
    const started = await fetch(`/attachments/${attachmentId}/multipart`, {
      method: "POST",
      credentials: "same-origin",
    });
    if (!started.ok) throw new Error("The upload could not be started.");
    const session = await started.json();
    const parts = [];
    let sent = 0;
    for (const part of session.parts) {
      const blob = file.slice(part.offset, part.offset + part.size);
      const etag = await putPart(part.url, blob);
      parts.push({ number: part.number, etag });
      sent += part.size;
      if (onProgress) onProgress(Math.min(1, sent / file.size));
    }
    const completed = await fetch(
      `/attachments/${attachmentId}/multipart/complete`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parts }),
      }
    );
    if (!completed.ok) throw new Error("The upload could not be completed.");
    return completed.json();
  }

  function uploadSingle(target, file, onProgress) {
    return new Promise((resolve, reject) => {
      const body = new FormData();
      Object.entries(target.fields).forEach(([name, value]) => body.append(name, value));
      body.append("file", file);
      const request = new XMLHttpRequest();
      request.open("POST", target.upload_url);
      request.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
      });
      request.addEventListener("load", () => {
        if (request.status >= 200 && request.status < 300) resolve();
        else reject(new Error("The upload did not reach storage."));
      });
      request.addEventListener("error", () => reject(new Error("The upload failed.")));
      request.send(body);
    });
  }

  // Resolves once the bytes are in storage. A small file takes one POST and still needs the
  // caller's finalize; a large one is split, and completing the last part also verifies and
  // scans it, so `finalized` is the attachment the caller can use immediately.
  async function uploadFile(target, file, onProgress) {
    if (target.multipart) {
      return { finalized: await uploadMultipart(target.attachment_id, file, onProgress) };
    }
    await uploadSingle(target, file, onProgress);
    return { finalized: null };
  }

  globalThis.SocialMediaOptimize = { shrinkImage, sha256, uploadFile };
})();
