/*!
 * Scribble rich-text editor (WS4 Phase A) — scribble/static/editor.js
 * =====================================================================
 *
 * PLAN.md §8 / plans/CONTRACTS.md §3 call for a TipTap/ProseMirror editor vendored as a prebuilt UMD
 * bundle under scribble/static/lib/. This environment cannot reach a package registry/CDN to fetch or
 * vet a TipTap bundle (offline build), so **this file ships a fallback**: a minimal, dependency-free
 * `contenteditable`-based editor that reads/writes the exact same ProseMirror-JSON contract
 * (scribble/content/schema.py) that TipTap would produce. Swapping in a real TipTap bundle later is a
 * drop-in — see "TIPTAP DROP-IN POINT" below — because everything outside this file (autosave API,
 * presence API, the content schema, the HTML renderer) already speaks ProseMirror JSON.
 *
 * ── FALLBACK EDITOR (this file), Phase A ─────────────────────────────────────────────────────────
 * - JSON -> DOM (`docToFragment`) and DOM -> JSON (`domToDoc`) walkers matching
 *   scribble/content/schema.py: doc/paragraph/text/heading/bulletList/orderedList/listItem/blockquote/
 *   codeBlock/hardBreak/image, plus the custom `variable` and `inlineImage` nodes (and `figure`).
 * - Marks (bold/italic/underline/strike/link) via `document.execCommand` — yes, deprecated, but broadly
 *   supported and adequate for a stopgap; replaced wholesale by TipTap's mark commands in the drop-in.
 * - Image paste/drop -> uploads via the artifact API, inserts an `inlineImage` node at the caret.
 * - `{{VARIABLE}}` chip insertion via a toolbar picker.
 * - Debounced autosave (POST ProseMirror doc JSON) + presence heartbeat polling (GET/POST presence).
 *
 * ── TIPTAP DROP-IN POINT ──────────────────────────────────────────────────────────────────────────
 * If/when a vendored TipTap bundle lands under scribble/static/lib/ (e.g. `tiptap.bundle.js`), have it
 * define `window.ScribbleTipTap = { mount(container, opts) { ... return handle; } }` where `opts` is
 * `{ findingId, block, apiBase, user, initialDoc, artifactUrl }` and `handle` exposes at least
 * `{ getDoc(), setDoc(doc), save(), destroy() }` (the same shape this file's fallback returns).
 * `ScribbleEditor.mount()` below already prefers `window.ScribbleTipTap` when present — no other file
 * needs to change. Load the TipTap bundle script *before* this file's mount() runs (e.g. before the
 * inline auto-mount script in scribble/templates/scribble/_editor.html, or call
 * `ScribbleEditor.mount(el)` yourself after the bundle is ready).
 */

(function (window, document) {
  "use strict";

  // ---------------------------------------------------------------------------- schema constants
  // Mirrors scribble/content/schema.py exactly (frozen contract — do not rename without updating both).
  var NODE = {
    DOC: "doc",
    PARAGRAPH: "paragraph",
    TEXT: "text",
    HEADING: "heading",
    BULLET_LIST: "bulletList",
    ORDERED_LIST: "orderedList",
    LIST_ITEM: "listItem",
    BLOCKQUOTE: "blockquote",
    CODE_BLOCK: "codeBlock",
    HARD_BREAK: "hardBreak",
    IMAGE: "image",
    VARIABLE: "variable",
    INLINE_IMAGE: "inlineImage",
    FIGURE: "figure",
  };

  var BLOCK_TAGS = { p: 1, div: 1, h1: 1, h2: 1, h3: 1, h4: 1, h5: 1, h6: 1, ul: 1, ol: 1, li: 1, blockquote: 1, pre: 1, figure: 1 };
  var MARK_TAGS = { strong: "bold", b: "bold", em: "italic", i: "italic", code: "code", s: "strike", strike: "strike", del: "strike", u: "underline", a: "link" };
  var MARK_OPEN_TAG = { bold: "strong", italic: "em", code: "code", strike: "s", underline: "u" };

  var AUTOSAVE_DEBOUNCE_MS = 800;
  var AUTOSAVE_RETRY_MS = 2500;
  var PRESENCE_HEARTBEAT_MS = 8000; // keep well under the server's presence TTL (~20s)

  // ---------------------------------------------------------------------------- JSON -> DOM

  function docToFragment(doc) {
    var frag = document.createDocumentFragment();
    var content = (doc && doc.content) || [];
    for (var i = 0; i < content.length; i++) {
      var el = blockNodeToDom(content[i]);
      if (el) frag.appendChild(el);
    }
    return frag;
  }

  function blockNodeToDom(node) {
    var tag, level, m;
    switch (node.type) {
      case NODE.PARAGRAPH:
        var p = document.createElement("p");
        appendInline(p, node.content);
        ensureNotEmpty(p);
        return p;
      case NODE.HEADING:
        level = clamp((node.attrs && node.attrs.level) || 2, 1, 6);
        var h = document.createElement("h" + level);
        appendInline(h, node.content);
        ensureNotEmpty(h);
        return h;
      case NODE.BULLET_LIST:
        return listToDom("ul", node);
      case NODE.ORDERED_LIST:
        return listToDom("ol", node);
      case NODE.LIST_ITEM:
        return listItemToDom(node);
      case NODE.BLOCKQUOTE:
        var bq = document.createElement("blockquote");
        appendBlocks(bq, node.content);
        return bq;
      case NODE.CODE_BLOCK:
        var pre = document.createElement("pre");
        var code = document.createElement("code");
        code.textContent = flatText(node);
        pre.appendChild(code);
        return pre;
      case NODE.FIGURE:
        var figure = document.createElement("figure");
        var children = node.content || [];
        for (var j = 0; j < children.length; j++) {
          var kid = inlineNodeToDom(children[j]);
          if (kid) figure.appendChild(kid);
        }
        var figcap = document.createElement("figcaption");
        figcap.textContent = (node.attrs && node.attrs.caption) || "";
        figure.appendChild(figcap);
        return figure;
      default:
        // Unknown/foreign node: never drop content — best-effort render its inline content as a paragraph.
        var fallback = document.createElement("p");
        appendInline(fallback, node.content || []);
        return fallback.childNodes.length ? fallback : null;
    }
  }

  function listToDom(tag, node) {
    var list = document.createElement(tag);
    var items = node.content || [];
    for (var i = 0; i < items.length; i++) {
      var li = blockNodeToDom(items[i]);
      if (li) list.appendChild(li);
    }
    return list;
  }

  function listItemToDom(node) {
    var li = document.createElement("li");
    appendBlocks(li, node.content);
    if (!li.childNodes.length) li.appendChild(document.createElement("br"));
    return li;
  }

  function appendBlocks(parent, content) {
    var items = content || [];
    for (var i = 0; i < items.length; i++) {
      var el = blockNodeToDom(items[i]);
      if (el) parent.appendChild(el);
    }
  }

  function appendInline(parent, content) {
    var items = content || [];
    for (var i = 0; i < items.length; i++) {
      var el = inlineNodeToDom(items[i]);
      if (el != null) parent.appendChild(el);
    }
  }

  function inlineNodeToDom(node) {
    if (!node) return null;
    if (node.type === NODE.TEXT) {
      var textNode = document.createTextNode(node.text || "");
      var marks = node.marks || [];
      var wrapped = textNode;
      for (var i = marks.length - 1; i >= 0; i--) {
        wrapped = wrapMark(marks[i], wrapped);
      }
      return wrapped;
    }
    if (node.type === NODE.HARD_BREAK) return document.createElement("br");
    if (node.type === NODE.VARIABLE) return variableChipEl((node.attrs && node.attrs.key) || "");
    if (node.type === NODE.INLINE_IMAGE) return inlineImageEl(node.attrs || {});
    if (node.type === NODE.IMAGE) {
      var attrs = node.attrs || {};
      var img = document.createElement("img");
      img.dataset.type = "image";
      img.src = attrs.src || "";
      img.alt = attrs.alt || "";
      img.contentEditable = "false";
      return img;
    }
    return null;
  }

  function wrapMark(mark, child) {
    if (mark.type === "link") {
      var a = document.createElement("a");
      a.href = (mark.attrs && mark.attrs.href) || "#";
      a.appendChild(child);
      return a;
    }
    var tag = MARK_OPEN_TAG[mark.type];
    if (!tag) return child;
    var el = document.createElement(tag);
    el.appendChild(child);
    return el;
  }

  function variableChipEl(key) {
    var span = document.createElement("span");
    span.className = "fr-var pill";
    span.contentEditable = "false";
    span.dataset.type = "variable";
    span.dataset.key = key;
    span.textContent = "{{" + key + "}}";
    return span;
  }

  function inlineImageEl(attrs, srcOverride) {
    var img = document.createElement("img");
    img.className = "fr-inline-image";
    img.dataset.type = "inlineImage";
    if (attrs.artifactId != null) img.dataset.artifactId = String(attrs.artifactId);
    if (attrs.caption) img.dataset.caption = attrs.caption;
    img.alt = attrs.alt || "";
    img.src = srcOverride || attrs.src || "";
    img.contentEditable = "false";
    return img;
  }

  function ensureNotEmpty(el) {
    if (!el.childNodes.length) el.appendChild(document.createElement("br"));
  }

  function flatText(node) {
    var out = [];
    var items = (node && node.content) || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].type === NODE.TEXT) out.push(items[i].text || "");
    }
    return out.join("");
  }

  function clamp(n, lo, hi) {
    n = parseInt(n, 10);
    if (isNaN(n)) return lo;
    return Math.max(lo, Math.min(hi, n));
  }

  // ---------------------------------------------------------------------------- DOM -> JSON

  function domToDoc(root) {
    var content = [];
    var inlineBuffer = [];

    function flushBuffer() {
      if (inlineBuffer.length) {
        content.push({ type: NODE.PARAGRAPH, content: mergeAdjacentText(inlineBuffer) });
        inlineBuffer = [];
      }
    }

    var children = root.childNodes;
    for (var i = 0; i < children.length; i++) {
      var node = children[i];
      if (node.nodeType === Node.TEXT_NODE) {
        if (node.textContent) inlineBuffer.push({ type: NODE.TEXT, text: node.textContent });
        continue;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      var tag = node.tagName.toLowerCase();
      if (!BLOCK_TAGS[tag]) {
        inlineBuffer = inlineBuffer.concat(inlineElementToNodes(node, []));
        continue;
      }
      flushBuffer();
      var blockNode = domBlockToNode(node);
      if (blockNode) content.push(blockNode);
    }
    flushBuffer();
    return { type: NODE.DOC, content: content };
  }

  function domBlockToNode(el) {
    var tag = el.tagName.toLowerCase();
    var hMatch = /^h([1-6])$/.exec(tag);
    if (tag === "p" || tag === "div") {
      return { type: NODE.PARAGRAPH, content: collectInline(el, []) };
    }
    if (hMatch) {
      return { type: NODE.HEADING, attrs: { level: parseInt(hMatch[1], 10) }, content: collectInline(el, []) };
    }
    if (tag === "ul") return { type: NODE.BULLET_LIST, content: mapListItems(el) };
    if (tag === "ol") return { type: NODE.ORDERED_LIST, content: mapListItems(el) };
    if (tag === "li") return domListItemToNode(el);
    if (tag === "blockquote") return { type: NODE.BLOCKQUOTE, content: domChildBlocks(el) };
    if (tag === "pre") {
      var codeEl = el.querySelector("code") || el;
      return { type: NODE.CODE_BLOCK, content: [{ type: NODE.TEXT, text: codeEl.textContent || "" }] };
    }
    if (tag === "figure") {
      var img = el.querySelector("img");
      var figcap = el.querySelector("figcaption");
      var figContent = img ? [domImageToNode(img)] : [];
      return { type: NODE.FIGURE, attrs: { caption: figcap ? figcap.textContent : "" }, content: figContent };
    }
    if (tag === "img") {
      return { type: NODE.PARAGRAPH, content: [domImageToNode(el)] };
    }
    // Unknown block element: descend as inline, never drop text.
    return { type: NODE.PARAGRAPH, content: collectInline(el, []) };
  }

  function mapListItems(listEl) {
    var out = [];
    var children = listEl.children || [];
    for (var i = 0; i < children.length; i++) {
      if (children[i].tagName && children[i].tagName.toLowerCase() === "li") {
        out.push(domListItemToNode(children[i]));
      }
    }
    return out;
  }

  function domListItemToNode(li) {
    var content = domChildBlocks(li);
    if (!content.length) content = [{ type: NODE.PARAGRAPH, content: collectInline(li, []) }];
    return { type: NODE.LIST_ITEM, content: content };
  }

  function domChildBlocks(el) {
    var out = [];
    var children = el.childNodes;
    for (var i = 0; i < children.length; i++) {
      var node = children[i];
      if (node.nodeType === Node.ELEMENT_NODE && BLOCK_TAGS[node.tagName.toLowerCase()]) {
        var n = domBlockToNode(node);
        if (n) out.push(n);
      }
    }
    return out;
  }

  function collectInline(el, marks) {
    var out = [];
    var children = el.childNodes;
    for (var i = 0; i < children.length; i++) {
      out = out.concat(inlineElementToNodes(children[i], marks));
    }
    return mergeAdjacentText(out);
  }

  function inlineElementToNodes(node, marks) {
    marks = marks || [];
    if (node.nodeType === Node.TEXT_NODE) {
      var text = node.textContent;
      if (!text) return [];
      var out = { type: NODE.TEXT, text: text };
      if (marks.length) out.marks = marks.map(cloneMark);
      return [out];
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return [];
    var tag = node.tagName.toLowerCase();
    if (tag === "br") return [{ type: NODE.HARD_BREAK }];
    if (tag === "span" && node.dataset && node.dataset.type === "variable") {
      return [{ type: NODE.VARIABLE, attrs: { key: node.dataset.key || "" } }];
    }
    if (tag === "img") {
      // Transient upload preview (WS14): never serialize it into content_json -- an inlineImage node
      // must not exist in the saved doc until its upload resolves with a real artifactId.
      if (node.dataset && node.dataset.frPreview) return [];
      return [domImageToNode(node)];
    }
    var markType = MARK_TAGS[tag];
    if (markType) {
      var mark = markType === "link" ? { type: "link", attrs: { href: node.getAttribute("href") || "#" } } : { type: markType };
      return collectInlineChildren(node, marks.concat([mark]));
    }
    // Unknown inline element (e.g. a stray <font>/<span> from pasted HTML): descend without a mark.
    return collectInlineChildren(node, marks);
  }

  function collectInlineChildren(el, marks) {
    var out = [];
    var children = el.childNodes;
    for (var i = 0; i < children.length; i++) {
      out = out.concat(inlineElementToNodes(children[i], marks));
    }
    return out;
  }

  function domImageToNode(img) {
    if ((img.dataset && (img.dataset.type === "inlineImage" || img.dataset.artifactId))) {
      var attrs = { alt: img.getAttribute("alt") || "" };
      if (img.dataset.artifactId) attrs.artifactId = parseInt(img.dataset.artifactId, 10);
      if (img.dataset.caption) attrs.caption = img.dataset.caption;
      return { type: NODE.INLINE_IMAGE, attrs: attrs };
    }
    return { type: NODE.IMAGE, attrs: { src: img.getAttribute("src") || "", alt: img.getAttribute("alt") || "" } };
  }

  function cloneMark(mark) {
    return mark.attrs ? { type: mark.type, attrs: mark.attrs } : { type: mark.type };
  }

  function mergeAdjacentText(nodes) {
    var out = [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var prev = out[out.length - 1];
      if (prev && prev.type === NODE.TEXT && n.type === NODE.TEXT && sameMarks(prev.marks, n.marks)) {
        prev.text += n.text;
      } else {
        out.push(n);
      }
    }
    return out;
  }

  function sameMarks(a, b) {
    a = a || [];
    b = b || [];
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (a[i].type !== b[i].type) return false;
      if (JSON.stringify(a[i].attrs || {}) !== JSON.stringify(b[i].attrs || {})) return false;
    }
    return true;
  }

  // ---------------------------------------------------------------------------- caret helpers

  function insertNodeAtCaret(root, node) {
    root.focus();
    var sel = window.getSelection();
    var range;
    if (sel && sel.rangeCount && root.contains(sel.anchorNode)) {
      range = sel.getRangeAt(0);
    } else {
      range = document.createRange();
      range.selectNodeContents(root);
      range.collapse(false);
    }
    range.deleteContents();
    range.insertNode(node);
    range.setStartAfter(node);
    range.collapse(true);
    if (sel) {
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }

  // ---------------------------------------------------------------------------- autosave + presence

  // Resilience (WS14, PLAN.md §19): a beforeunload guard must be armed for as long as there's
  // unsaved text -- from the moment an edit makes `state.dirty` true until a save actually lands
  // successfully with no newer edit pending behind it. ScribbleOutbox (scribble/static/outbox.js)
  // owns the single shared beforeunload listener; editor.js just reports into it via
  // `setExternalPending(key, bool)` so image-upload-pending and text-save-pending share one guard
  // instead of each subsystem wiring its own `beforeunload` handler.
  function pendingKey(state) {
    return "fr-editor:" + state.findingId + ":" + state.block;
  }

  function armPendingGuard(state) {
    if (window.ScribbleOutbox) window.ScribbleOutbox.setExternalPending(pendingKey(state), true);
  }

  function disarmPendingGuard(state) {
    if (window.ScribbleOutbox) window.ScribbleOutbox.setExternalPending(pendingKey(state), false);
  }

  function scheduleSave(state) {
    state.dirty = true;
    armPendingGuard(state);
    setStatus(state, "editing…");
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(function () {
      saveNow(state);
    }, AUTOSAVE_DEBOUNCE_MS);
  }

  function saveNow(state) {
    if (!state.dirty) return;
    var doc = domToDoc(state.editableEl);
    state.dirty = false;
    setStatus(state, "saving…");
    fetch(state.apiBase + "/findings/" + state.findingId + "/blocks/" + encodeURIComponent(state.block), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    })
      .then(function (res) {
        if (!res.ok) throw new Error("autosave failed: HTTP " + res.status);
        return res.json();
      })
      .then(function () {
        setStatus(state, "saved " + nowTime());
        // Only disarm if nothing newer snuck in while this request was in flight -- the *next*
        // scheduleSave() call (which already re-armed the guard) is what will eventually clear it.
        if (!state.dirty) disarmPendingGuard(state);
      })
      .catch(function (err) {
        console.error("[scribble-editor] autosave failed", err);
        setStatus(state, "save failed — retrying…", true);
        state.dirty = true; // guard stays armed -- there is still unsaved content
        clearTimeout(state.saveTimer);
        state.saveTimer = setTimeout(function () {
          saveNow(state);
        }, AUTOSAVE_RETRY_MS);
      });
  }

  function fetchLatest(state) {
    fetch(state.apiBase + "/findings/" + state.findingId + "/blocks/" + encodeURIComponent(state.block), {
      credentials: "same-origin",
    })
      .then(function (res) {
        return res.ok ? res.json() : null;
      })
      .then(function (data) {
        if (data && data.doc) {
          state.editableEl.innerHTML = "";
          state.editableEl.appendChild(docToFragment(data.doc));
          ensureNotEmpty(state.editableEl);
        }
      })
      .catch(function () {
        /* keep the embedded/initial doc if the round trip fails */
      });
  }

  function startPresence(state) {
    var url = state.apiBase + "/findings/" + state.findingId + "/blocks/" + encodeURIComponent(state.block) + "/presence";
    function beat() {
      fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user: state.user }),
      })
        .then(function (res) {
          return res.ok ? res.json() : null;
        })
        .then(function (data) {
          if (data && data.editors) renderPresence(state, data.editors);
        })
        .catch(function () {});
    }
    beat();
    state.presenceTimer = setInterval(beat, PRESENCE_HEARTBEAT_MS);

    function leave() {
      var payload = JSON.stringify({ user: state.user, leave: true });
      // fetch(..., {keepalive: true}) (not sendBeacon): sendBeacon can't set custom headers, so under
      // a CSRF-enforcing host (Lotek wraps window.fetch to attach X-CSRFToken; see
      // docs/LOTEK_ADOPTION.md §4.2) a beacon-based leave 400s. `keepalive: true` gets the same
      // "survives pagehide/unload" guarantee sendBeacon has, while still going through the host's
      // fetch shim. Only fall back to sendBeacon if fetch itself is unavailable (never true in any
      // target browser -- this is a defensive last resort, not the primary path).
      if (typeof fetch === "function") {
        fetch(url, {
          method: "POST",
          body: payload,
          headers: { "Content-Type": "application/json" },
          keepalive: true,
          credentials: "same-origin",
        }).catch(function () {});
      } else if (navigator.sendBeacon) {
        navigator.sendBeacon(url, new Blob([payload], { type: "application/json" }));
      }
    }
    window.addEventListener("pagehide", leave);
    state.cleanupPresence = function () {
      clearInterval(state.presenceTimer);
      window.removeEventListener("pagehide", leave);
      leave();
    };
  }

  function renderPresence(state, editors) {
    var others = [];
    for (var i = 0; i < editors.length; i++) {
      if (editors[i].user !== state.user) others.push(editors[i].user);
    }
    if (!others.length) {
      state.presenceEl.textContent = "";
      state.presenceEl.hidden = true;
      return;
    }
    state.presenceEl.hidden = false;
    state.presenceEl.textContent = others.join(", ") + (others.length > 1 ? " are editing" : " is editing");
  }

  // ---------------------------------------------------------------------------- image upload

  function handlePaste(state, ev) {
    var items = (ev.clipboardData && ev.clipboardData.items) || [];
    var imageItem = null;
    for (var i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.indexOf("image/") === 0) {
        imageItem = items[i];
        break;
      }
    }
    if (!imageItem) return; // let default (rich/plain text) paste happen
    ev.preventDefault();
    var file = imageItem.getAsFile();
    if (file) uploadAndInsertImage(state, file);
  }

  function handleDrop(state, ev) {
    var files = (ev.dataTransfer && ev.dataTransfer.files) || [];
    var imageFile = null;
    for (var i = 0; i < files.length; i++) {
      if (files[i].type && files[i].type.indexOf("image/") === 0) {
        imageFile = files[i];
        break;
      }
    }
    if (!imageFile) return;
    ev.preventDefault();
    uploadAndInsertImage(state, imageFile);
  }

  // Inline image paste/drop: CORRECT + ONLINE-ONLY (WS14 review scope decision, PLAN.md §19).
  //
  // The hard invariant this design enforces: **`content_json` must never contain an `inlineImage`
  // node without a real `artifactId`**. A blank inlineImage would bake an empty <img> into the saved
  // finding and every rendered report. So on paste/drop we insert only a *transient preview* node
  // (marked `data-fr-preview`, which the DOM->JSON serializer skips entirely -- see
  // `inlineElementToNodes`), and we promote it into a real, serializable `inlineImage` node ONLY once
  // the upload has RESOLVED with a real artifact id. If the upload ultimately fails, the preview is
  // removed and nothing is persisted.
  //
  // The upload still rides the resilience outbox for in-session retry-with-backoff while online. What
  // this path deliberately does NOT do is durable-across-reload temp-id -> real-id reconciliation for
  // *inline* images: after a reload there is no editor node to promote, so a still-queued inline op
  // that completes post-reload simply lands as a normal attached artifact (nothing is lost, but it
  // won't re-insert itself inline). That durable inline story is deferred to Lotek's Yjs editor swap
  // (see plans/feat-ws14-resilience.md); the gallery/attached path remains fully offline-durable.

  // tempId -> { state, previewImg }. Only lives for the in-session lifetime of one upload; a reload
  // clears it (and the transient preview node) by design.
  var pendingImageUploads = {};

  function makePreviewImg(objectUrl, alt) {
    var img = document.createElement("img");
    img.className = "fr-inline-image fr-inline-image-pending";
    // data-fr-preview marks this node as a NON-serializable transient (domToDoc drops it). Note we do
    // NOT set data-type="inlineImage" here, so even if the preview guard were bypassed it still has no
    // artifactId to serialize -- defense in depth for the "never persist a blank inlineImage" invariant.
    img.dataset.frPreview = "1";
    img.alt = alt || "";
    img.src = objectUrl || "";
    img.contentEditable = "false";
    img.title = "Uploading…";
    return img;
  }

  function uploadAndInsertImage(state, file) {
    if (!window.ScribbleOutbox) {
      setStatus(state, "image upload unavailable (resilience outbox missing)", true);
      return;
    }
    var tempId = "img-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    var objectUrl = URL.createObjectURL(file);
    var previewImg = makePreviewImg(objectUrl, file.name || "");
    previewImg.dataset.tempId = tempId;
    insertNodeAtCaret(state.editableEl, previewImg);
    pendingImageUploads[tempId] = { state: state, previewImg: previewImg, objectUrl: objectUrl };
    setStatus(state, "uploading image…");

    window.ScribbleOutbox.enqueueUpload({
      tempId: tempId,
      url: state.apiBase + "/artifacts",
      blob: file,
      filename: file.name || "pasted-image.png",
      fields: {
        placement: "inline", // NOTE: server currently ignores this and stores 'attached' (driver N1)
        engagement_id: state.engagementId != null ? String(state.engagementId) : "",
        finding_id: state.findingId != null ? String(state.findingId) : "",
      },
    });
  }

  function revokePreview(meta) {
    if (meta && meta.objectUrl) {
      try {
        URL.revokeObjectURL(meta.objectUrl);
      } catch (e) {
        /* already revoked or invalid -- nothing to clean up */
      }
    }
  }

  // Promote the transient preview into a real, serializable inlineImage node carrying the artifact id,
  // then autosave. This is the ONLY point at which an inlineImage enters the serialized document, so it
  // always has a real artifactId.
  function resolveInlineImage(meta, data) {
    var preview = meta.previewImg;
    if (!preview || !preview.parentNode) {
      // The node was removed before the upload resolved (editor destroyed / user deleted it). Nothing
      // to promote inline; the artifact still persisted server-side (as an attached artifact).
      revokePreview(meta);
      return;
    }
    var real = inlineImageEl(
      { artifactId: data.id, alt: preview.alt || "" },
      data.url || (meta.state.artifactUrl ? meta.state.artifactUrl(data.id) : "")
    );
    preview.parentNode.replaceChild(real, preview);
    revokePreview(meta);
    setStatus(meta.state, "image inserted");
    scheduleSave(meta.state); // persist the now-complete inlineImage (with a real artifactId)
  }

  // Failure is terminal for inline paste (correct + online-only): remove the transient preview so the
  // document is exactly as it was before the paste, and persist nothing. The user can re-paste to try
  // again. (The outbox has already retried with backoff up to its cap before firing this.)
  function failInlineImage(meta, error) {
    var detail =
      (error && error.body && error.body.error) ||
      (error && error.status && "HTTP " + error.status) ||
      (error && error.message) ||
      "upload failed";
    if (meta.previewImg && meta.previewImg.parentNode) {
      meta.previewImg.parentNode.removeChild(meta.previewImg);
    }
    revokePreview(meta);
    setStatus(meta.state, "image upload failed (" + detail + ") — nothing was saved; paste again to retry", true);
  }

  // Registered once per page (guarded below), not once per mounted editor instance: editor.js's
  // <script> tag is re-executed once per content block (finding.html mounts one _editor.html include
  // per block), and ScribbleOutbox's events are global, so a per-instance registration would fire the
  // same reconciliation N times over. Keying off `pendingImageUploads[tempId]` scopes each event to the
  // one paste it belongs to regardless of how many editor blocks are mounted on the page.
  if (window.ScribbleOutbox && !window.__scribbleEditorOutboxWired) {
    window.__scribbleEditorOutboxWired = true;

    window.ScribbleOutbox.on("resolved", function (tempId, data) {
      var meta = pendingImageUploads[tempId];
      if (!meta) return; // not one of ours (e.g. a gallery upload) or already handled
      delete pendingImageUploads[tempId];
      resolveInlineImage(meta, data);
    });

    window.ScribbleOutbox.on("failed", function (tempId, error) {
      var meta = pendingImageUploads[tempId];
      if (!meta) return;
      delete pendingImageUploads[tempId];
      failInlineImage(meta, error);
    });
  }

  // ---------------------------------------------------------------------------- toolbar

  function buildToolbar(state, variableKeys) {
    var bar = document.createElement("div");
    bar.className = "fr-editor-toolbar";

    var markButtons = [
      ["B", "bold", "Bold"],
      ["I", "italic", "Italic"],
      ["U", "underline", "Underline"],
      ["S", "strikeThrough", "Strikethrough"],
    ];
    markButtons.forEach(function (spec) {
      bar.appendChild(makeToolbarButton(spec[0], spec[2], function () {
        document.execCommand(spec[1]);
        scheduleSave(state);
      }));
    });

    var listButtons = [
      ["• List", "insertUnorderedList", "Bulleted list"],
      ["1. List", "insertOrderedList", "Numbered list"],
    ];
    listButtons.forEach(function (spec) {
      bar.appendChild(makeToolbarButton(spec[0], spec[2], function () {
        document.execCommand(spec[1]);
        scheduleSave(state);
      }));
    });

    var imgBtn = makeToolbarButton("🖼 Image", "Insert image", function () {
      fileInput.click();
    });
    var fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = "image/*";
    fileInput.hidden = true;
    fileInput.addEventListener("change", function () {
      var file = fileInput.files && fileInput.files[0];
      if (file) uploadAndInsertImage(state, file);
      fileInput.value = "";
    });
    bar.appendChild(imgBtn);
    bar.appendChild(fileInput);

    bar.appendChild(buildVariablePicker(state, variableKeys));
    return bar;
  }

  function makeToolbarButton(label, title, onClick) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn fr-editor-btn";
    btn.textContent = label;
    btn.title = title;
    // Prevent the button from stealing focus/selection away from the editable surface.
    btn.addEventListener("mousedown", function (e) {
      e.preventDefault();
    });
    btn.addEventListener("click", onClick);
    return btn;
  }

  function buildVariablePicker(state, keys) {
    var select = document.createElement("select");
    select.className = "fr-editor-varpicker";
    var placeholder = document.createElement("option");
    placeholder.textContent = "Insert variable…";
    placeholder.value = "";
    select.appendChild(placeholder);
    (keys || []).forEach(function (key) {
      var opt = document.createElement("option");
      opt.value = key;
      opt.textContent = "{{" + key + "}}";
      select.appendChild(opt);
    });
    select.addEventListener("mousedown", function (e) {
      e.stopPropagation();
    });
    select.addEventListener("change", function () {
      var key = select.value;
      select.value = "";
      if (!key) return;
      insertNodeAtCaret(state.editableEl, variableChipEl(key));
      scheduleSave(state);
    });
    return select;
  }

  // ---------------------------------------------------------------------------- misc helpers

  function setStatus(state, text, isError) {
    if (!state.statusEl) return;
    state.statusEl.textContent = text;
    state.statusEl.classList.toggle("fr-editor-status-error", !!isError);
  }

  function nowTime() {
    return new Date().toTimeString().slice(0, 8);
  }

  function getAnonUser() {
    try {
      var key = "scribble-anon-user";
      var id = window.sessionStorage.getItem(key);
      if (!id) {
        id = "anon-" + Math.random().toString(36).slice(2, 8);
        window.sessionStorage.setItem(key, id);
      }
      return id;
    } catch (e) {
      return "anon";
    }
  }

  function readEmbeddedDoc(container) {
    var script = container.querySelector("script.scribble-editor-doc-data");
    if (!script) return null;
    try {
      return JSON.parse(script.textContent || "null");
    } catch (e) {
      return null;
    }
  }

  function readVariableKeys(container) {
    var script = container.querySelector("script.scribble-editor-vars-data");
    if (!script) return null;
    try {
      return JSON.parse(script.textContent || "null");
    } catch (e) {
      return null;
    }
  }

  // ---------------------------------------------------------------------------- mount

  function mountFallbackEditor(container, opts) {
    container.classList.add("fr-editor-mounted");
    container.innerHTML = "";

    var state = {
      findingId: opts.findingId,
      engagementId: opts.engagementId,
      block: opts.block,
      apiBase: opts.apiBase,
      user: opts.user,
      artifactUrl: opts.artifactUrl,
      dirty: false,
      saveTimer: null,
    };

    container.appendChild(buildToolbar(state, opts.variableKeys));

    var editableEl = document.createElement("div");
    editableEl.className = "fr-editor-surface";
    editableEl.contentEditable = "true";
    editableEl.spellcheck = true;
    editableEl.appendChild(docToFragment(opts.initialDoc || { type: NODE.DOC, content: [] }));
    ensureNotEmpty(editableEl);
    container.appendChild(editableEl);
    state.editableEl = editableEl;

    var statusEl = document.createElement("div");
    statusEl.className = "fr-editor-status muted";
    statusEl.textContent = "ready";
    container.appendChild(statusEl);
    state.statusEl = statusEl;

    var presenceEl = document.createElement("div");
    presenceEl.className = "fr-editor-presence muted";
    presenceEl.hidden = true;
    container.appendChild(presenceEl);
    state.presenceEl = presenceEl;

    try {
      document.execCommand("defaultParagraphSeparator", false, "p");
    } catch (e) {
      /* older browsers: ignore, fallback DOM parsing tolerates <div> too */
    }

    editableEl.addEventListener("input", function () {
      scheduleSave(state);
    });
    editableEl.addEventListener("paste", function (ev) {
      handlePaste(state, ev);
    });
    editableEl.addEventListener("drop", function (ev) {
      handleDrop(state, ev);
    });
    editableEl.addEventListener("dragover", function (ev) {
      ev.preventDefault();
    });

    if (state.findingId != null && !opts.initialDoc) fetchLatest(state);
    if (state.findingId != null) startPresence(state);

    return {
      getDoc: function () {
        return domToDoc(state.editableEl);
      },
      setDoc: function (doc) {
        editableEl.innerHTML = "";
        editableEl.appendChild(docToFragment(doc));
        ensureNotEmpty(editableEl);
      },
      save: function () {
        scheduleSave(state);
        saveNow(state);
      },
      destroy: function () {
        // N7: don't silently drop an edit that was mid-debounce when the editor is torn down --
        // flush it. saveNow() clears the guard on success (and its own retry keeps trying on
        // failure); only disarm here when there's nothing left to save.
        if (state.dirty) {
          clearTimeout(state.saveTimer);
          saveNow(state);
        } else {
          clearTimeout(state.saveTimer);
          disarmPendingGuard(state);
        }
        if (state.cleanupPresence) state.cleanupPresence();
      },
    };
  }

  function mount(container, options) {
    if (!container) throw new Error("ScribbleEditor.mount: container element required");
    options = options || {};
    var ds = container.dataset || {};

    var engagementId =
      options.engagementId != null ? options.engagementId : parseInt(ds.engagementId, 10);
    var opts = {
      findingId: options.findingId != null ? options.findingId : parseInt(ds.findingId, 10),
      // engagement_id is REQUIRED by POST /artifacts (create_artifact 400s without it), so inline
      // image paste is broken unless the mount threads it through -- see _editor.html data-engagement-id.
      engagementId: isNaN(engagementId) ? null : engagementId,
      block: options.block || ds.block,
      apiBase: options.apiBase || ds.apiBase || "/scribble/api",
      user: options.user || ds.user || getAnonUser(),
      initialDoc: options.doc || readEmbeddedDoc(container),
      variableKeys: options.variableKeys || readVariableKeys(container),
    };
    opts.artifactUrl = options.artifactUrl || function (id) {
      return opts.apiBase + "/artifacts/" + id + "/raw";
    };

    // TIPTAP DROP-IN POINT: prefer a vendored TipTap bundle if one has registered itself.
    if (window.ScribbleTipTap && typeof window.ScribbleTipTap.mount === "function") {
      return window.ScribbleTipTap.mount(container, opts);
    }
    return mountFallbackEditor(container, opts);
  }

  window.ScribbleEditor = {
    mount: mount,
    // Exposed for tests/tooling/debugging and for a future TipTap adapter that wants to reuse the
    // JSON<->DOM walkers instead of reimplementing them.
    _internal: { docToFragment: docToFragment, domToDoc: domToDoc, NODE: NODE },
  };
})(window, document);
