/* Scribble "Rephrase with AI" (Phase 3 of the lotek AI engine).
 *
 * Streams a rephrased draft for a finding block into a preview the operator reviews and copies into the
 * editor — the human stays in the loop, this never writes content_json. Uses window.fetch so lotek's
 * CSRF wrapper attaches X-CSRFToken on the cookie-authed surface. The route (draft_api.py) relays the
 * host `ai_stream` hook and fails closed to inline text (e.g. when AI completion is disabled), which we
 * simply render.
 *
 * Loaded per content block (like editor.js) but guards itself against double-wiring.
 */
(function () {
  if (window.__scribbleRephraseWired) return;
  window.__scribbleRephraseWired = true;

  function wire(box) {
    if (box.dataset.rephraseWired) return;
    box.dataset.rephraseWired = "1";
    var startBtn = box.querySelector('[data-action="scribble-rephrase-start"]');
    var copyBtn = box.querySelector('[data-action="scribble-rephrase-copy"]');
    var out = box.querySelector(".scribble-rephrase-out");
    var status = box.querySelector(".scribble-rephrase-status");
    if (!startBtn || !out) return;

    startBtn.addEventListener("click", async function () {
      var base = box.dataset.apiBase || "/scribble/api";
      var url =
        base + "/findings/" + box.dataset.findingId + "/rephrase/" + encodeURIComponent(box.dataset.block);
      out.textContent = "";
      out.style.display = "block";
      if (copyBtn) copyBtn.style.display = "none";
      if (status) status.textContent = "rephrasing… (a slow model can take ~a minute)";
      startBtn.disabled = true;
      try {
        var resp = await window.fetch(url, { method: "POST", credentials: "same-origin" });
        if (!resp.body) {
          out.textContent = "[HTTP " + resp.status + "]";
          return;
        }
        // The body is our text stream on success AND the inline error text on 503/404 — read it either way.
        var reader = resp.body.getReader();
        var dec = new TextDecoder();
        while (true) {
          var chunk = await reader.read();
          if (chunk.done) break;
          out.textContent += dec.decode(chunk.value, { stream: true });
        }
      } catch (e) {
        out.textContent += "\n[request failed: " + e + "]";
      } finally {
        startBtn.disabled = false;
        if (status) status.textContent = "";
        if (copyBtn && out.textContent.trim()) copyBtn.style.display = "";
      }
    });

    if (copyBtn) {
      copyBtn.addEventListener("click", function () {
        var text = out.textContent || "";
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text);
          var prev = copyBtn.textContent;
          copyBtn.textContent = "Copied";
          setTimeout(function () {
            copyBtn.textContent = prev;
          }, 1500);
        }
      });
    }
  }

  function wireAll() {
    document.querySelectorAll("[data-scribble-rephrase]").forEach(wire);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireAll);
  } else {
    wireAll();
  }
})();
