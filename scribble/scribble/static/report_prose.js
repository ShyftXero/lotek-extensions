/* Report-prose editor (ext#346 item 4): mounts the SHARED kit reporting editor over each standing-prose
 * field in FORM-SUBMIT mode. The <textarea> stays the real posted field — hidden once the editor mounts,
 * filled with the editor's doc serialized back to plain text on submit, so the HTML/DOCX renderers (which
 * split methodology_text/scope_limitations_text on blank lines) and the save-path dedup
 * (scribble.prose.override_for_storage) are unchanged. With JS off, the textarea is a plain edit/save box.
 *
 * "Reset to standard" clears the field (Save then stores NULL = the standing text); "Rephrase with AI"
 * sends the current draft to the host AI hook and drops the rewrite back for the operator to review before
 * Save (never a silent overwrite). Both operate on the editor when it mounted, else on the textarea.
 * CSP-strict: external classic script, no inline handlers. */
(function () {
  "use strict";
  var form = document.querySelector("[data-prose-form]");
  if (!form) return;
  var rephraseUrl = form.getAttribute("data-rephrase-url");
  var status = form.querySelector("[data-prose-status]");

  function setStatus(msg) {
    if (status) status.textContent = msg || "";
  }

  // --- plain text <-> ProseMirror doc. The SAME paragraph model the renderers split on: a blank line is
  //     a paragraph boundary ("\n\n"), a single newline is a hard break ("\n"). This is the one home for
  //     the round-trip, so prefill, serialize-on-submit and rephrase can't drift.
  function textToDoc(text) {
    var content = [];
    (text || "").split("\n\n").forEach(function (para) {
      var lines = para.split("\n");
      var inline = [];
      lines.forEach(function (line, i) {
        if (line) inline.push({ type: "text", text: line });
        if (i < lines.length - 1) inline.push({ type: "hardBreak" });
      });
      content.push({ type: "paragraph", content: inline });
    });
    return { type: "doc", content: content.length ? content : [{ type: "paragraph", content: [] }] };
  }

  function inlineText(node) {
    if (!node) return "";
    if (node.type === "text") return node.text || "";
    if (node.type === "hardBreak") return "\n";
    return (node.content || []).map(inlineText).join("");
  }

  function blockText(node) {
    // Lists expand to one line per item; everything else is its inline text.
    if (node.type === "bulletList" || node.type === "orderedList" || node.type === "listItem") {
      return (node.content || []).map(blockText).join("\n");
    }
    return inlineText(node);
  }

  function docToText(doc) {
    return ((doc && doc.content) || [])
      .map(blockText)
      .filter(function (t) { return t.trim() !== ""; })
      .join("\n\n");
  }

  // --- mount the editor over each field (form-submit mode: no finding-id -> autosave loop stays off).
  var editors = {}; // textarea id -> { handle, textarea }
  var Kit = window.LotekReportingEditor;
  if (Kit) {
    form.querySelectorAll("[data-rp-editor]").forEach(function (el) {
      if (el.dataset.rpMounted) return;
      el.dataset.rpMounted = "1";
      var ta = el.parentNode.querySelector("[data-rp-body]");
      if (!ta) return;
      var handle = Kit.mount(el, { doc: textToDoc(ta.value) });
      ta.style.display = "none"; // JS present: the rich editor is the input; hide the fallback
      editors[ta.id] = { handle: handle, textarea: ta };
    });
    form.addEventListener("submit", function () {
      Object.keys(editors).forEach(function (id) {
        editors[id].textarea.value = docToText(editors[id].handle.getDoc());
      });
    });
  }

  function setField(id, text) {
    var e = editors[id];
    if (e) {
      e.handle.setDoc(textToDoc(text));
      e.textarea.value = text;
    } else {
      var ta = document.getElementById(id);
      if (ta) ta.value = text;
    }
  }

  function getField(id) {
    var e = editors[id];
    if (e) return docToText(e.handle.getDoc());
    var ta = document.getElementById(id);
    return ta ? ta.value : "";
  }

  form.addEventListener("click", function (ev) {
    var reset = ev.target.closest ? ev.target.closest("[data-reset]") : null;
    if (reset) {
      setField(reset.getAttribute("data-reset"), "");
      setStatus("Cleared — Save to use the standard text.");
      return;
    }
    var button = ev.target.closest ? ev.target.closest("[data-rephrase]") : null;
    if (!button || !rephraseUrl) return;
    var id = button.getAttribute("data-rephrase");
    var text = getField(id).trim();
    if (!text) {
      setStatus("Nothing to rephrase.");
      return;
    }
    var fieldEl = document.getElementById(id);
    button.disabled = true;
    setStatus("Rephrasing…");
    fetch(rephraseUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ field: fieldEl ? fieldEl.getAttribute("data-field") : "", text: text }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (res.ok && res.j && res.j.text) {
          setField(id, res.j.text);
          setStatus("Rephrased — review it, then Save.");
        } else {
          setStatus((res.j && res.j.detail) || "Rephrase failed.");
        }
      })
      .catch(function () { setStatus("Rephrase failed."); })
      .then(function () { button.disabled = false; });
  });
})();
