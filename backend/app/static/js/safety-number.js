// Per-call safety numbers.
//
// Both sides of a peer connection derive the number from the two DTLS fingerprints that are
// actually protecting the media, so a signaling server (or anyone between the two browsers)
// that substitutes a key produces a number the participants can see is wrong. The derivation
// is symmetric: sort the two fingerprints, hash them, and render 16 decimal digits. The file is
// a plain script in the browser and a CommonJS module under Node, so the same algorithm is what
// the tests exercise.
(() => {
  "use strict";

  const FINGERPRINT_PATTERN = /^a=fingerprint:sha-256 ([0-9A-F:]+)$/im;
  const DIGITS_MODULUS = 10000000000000000n;

  function fingerprint(sdp) {
    const match = String(sdp || "").match(FINGERPRINT_PATTERN);
    return match ? match[1].toUpperCase() : "";
  }

  async function safetyNumber(localSdp, remoteSdp) {
    const prints = [fingerprint(localSdp), fingerprint(remoteSdp)].filter(Boolean).sort();
    if (prints.length < 2) return null;
    const subtle = globalThis.crypto?.subtle;
    if (!subtle) return null;
    const digest = await subtle.digest("SHA-256", new TextEncoder().encode(prints.join(":")));
    const bytes = Array.from(new Uint8Array(digest).slice(0, 8));
    const value = bytes.reduce((accumulator, byte) => (accumulator << 8n) + BigInt(byte), 0n);
    return (value % DIGITS_MODULUS).toString().padStart(16, "0");
  }

  function format(digits) {
    if (!digits) return "";
    return [0, 4, 8, 12].map((start) => digits.slice(start, start + 4)).join(" ");
  }

  const api = { fingerprint, safetyNumber, format };
  globalThis.SocialSafetyNumber = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
