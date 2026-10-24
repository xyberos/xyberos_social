// Progressive enhancement for the post composer.
//
// The box grows with the draft, a quiet character count appears once there is text, and
// Ctrl/Cmd+Enter submits. The Post button only waits for content when this file runs; the
// server still makes the final decision, and without JavaScript the plain form submits.
(() => {
  const composer = document.querySelector(".post-compose.composer");
  if (!composer) return;

  const text = composer.querySelector("[data-composer-text]");
  const headline = composer.querySelector(".composer-headline");
  const counter = composer.querySelector("[data-composer-count]");
  const post = composer.querySelector(".composer-post");
  const mediaList = composer.querySelector("[data-media-list]");
  const limit = Number(text?.maxLength) || 10000;

  function resize() {
    if (!text) return;
    text.style.height = "auto";
    text.style.height = `${Math.min(text.scrollHeight, 360)}px`;
  }

  function updateCount() {
    if (!counter || !text) return;
    const used = text.value.length;
    counter.hidden = used === 0;
    counter.textContent = `${used.toLocaleString()} / ${limit.toLocaleString()}`;
    counter.classList.toggle("is-near", used > limit * 0.9);
  }

  function updatePostState() {
    if (!post) return;
    // A headline alone is not a post: the server wants text or an attachment.
    const hasText = Boolean(text?.value.trim());
    const hasMedia = Boolean(mediaList && mediaList.children.length);
    post.disabled = !hasText && !hasMedia;
  }

  text?.addEventListener("input", () => {
    resize();
    updateCount();
    updatePostState();
  });
  // The headline does not change whether a post can be published, but the listener keeps the
  // state current if the body was changed by an autofill or another script.
  headline?.addEventListener("input", updatePostState);

  text?.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      if (post && !post.disabled) composer.requestSubmit();
    }
  });

  if (mediaList) {
    new MutationObserver(updatePostState).observe(mediaList, { childList: true });
  }

  resize();
  updateCount();
  updatePostState();
})();
