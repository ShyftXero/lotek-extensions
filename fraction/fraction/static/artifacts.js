// fraction/static/artifacts.js — vanilla JS behavior for the artifact gallery partial (WS5).
//
// No build step, no external library (Lotek is CSP-strict: no CDN scripts). Uses HTML5 drag-and-drop
// events for reordering and event delegation on `document` so this file is safe to include more than
// once on a page (one gallery per finding) without double-binding handlers.
(function () {
  "use strict";

  if (window.__fractionArtifactsInit) return;
  window.__fractionArtifactsInit = true;

  function jsonHeaders() {
    return { "Content-Type": "application/json" };
  }

  function galleryOf(el) {
    return el.closest(".fraction-gallery");
  }

  function isImageArtifact(artifact) {
    return artifact.kind === "screenshot" || (artifact.content_type || "").indexOf("image/") === 0;
  }

  // Builds a gallery <li> matching the server-rendered markup in _gallery.html, so a freshly uploaded
  // artifact appears immediately without a full page reload.
  //
  // `pendingInfo`, when given, means this row represents an upload still in flight through the
  // resilience outbox (fraction/static/outbox.js) rather than a real, persisted Artifact: there is no
  // id/update_url/delete_url yet, the thumbnail (if any) is a local `object-URL` preview, and the row
  // carries a status line instead of the include-toggle. `reconcileResolved`/`markFailed` below mutate
  // this same DOM node in place once the outbox settles, so the caption input / include checkbox /
  // delete button the row already contains just start working once a real artifact id lands.
  function buildItem(artifact, pendingInfo) {
    const li = document.createElement("li");
    li.className = "fraction-gallery-item";
    li.draggable = !pendingInfo;
    if (pendingInfo) {
      li.classList.add("is-pending");
      li.dataset.tempId = pendingInfo.tempId;
    } else {
      li.dataset.id = String(artifact.id);
      li.dataset.updateUrl = artifact.update_url;
      li.dataset.deleteUrl = artifact.delete_url;
      li.dataset.rawUrl = artifact.url;
    }

    const showImage = pendingInfo ? !!pendingInfo.objectUrl : isImageArtifact(artifact);
    const media = showImage
      ? '<a class="fraction-gallery-thumb" target="_blank" rel="noopener">' +
        '<img alt="" loading="lazy" /></a>'
      : '<a class="fraction-gallery-file-icon pill" target="_blank" rel="noopener"></a>';

    li.innerHTML =
      '<span class="fraction-gallery-handle mono" title="Drag to reorder">&#x2837;</span>' +
      media +
      '<div class="fraction-gallery-meta">' +
      '<div class="fraction-gallery-filename mono"></div>' +
      '<input type="text" class="fraction-gallery-caption" placeholder="Caption" />' +
      '<div class="fraction-gallery-pending-status muted"></div>' +
      "</div>" +
      '<label class="fraction-gallery-toggle">' +
      '<input type="checkbox" class="fraction-gallery-include" checked /> include' +
      "</label>" +
      '<button type="button" class="btn fraction-gallery-delete" title="Delete artifact">&times;</button>';

    const link = li.querySelector("a");
    const img = li.querySelector("img");
    const statusEl = li.querySelector(".fraction-gallery-pending-status");
    li.querySelector(".fraction-gallery-filename").textContent = artifact.filename || "";
    li.querySelector(".fraction-gallery-caption").value = artifact.caption || "";

    if (pendingInfo) {
      link.removeAttribute("href");
      link.removeAttribute("target");
      if (img) {
        img.src = pendingInfo.objectUrl || "";
        img.alt = artifact.caption || artifact.filename || "";
      }
      statusEl.textContent = pendingInfo.statusText || "Uploading…";
      li.querySelector(".fraction-gallery-toggle").hidden = true;
      li.querySelector(".fraction-gallery-delete").title = "Cancel upload";
      return li;
    }

    link.href = artifact.url;
    if (img) {
      img.src = artifact.url;
      img.alt = artifact.caption || artifact.filename || "";
    } else {
      link.textContent = artifact.kind || "file";
    }
    li.querySelector(".fraction-gallery-include").checked = !!artifact.include_in_report;
    if (!artifact.include_in_report) li.classList.add("is-excluded");
    statusEl.remove();
    return li;
  }

  function revokeObjectUrl(li) {
    if (li._objectUrl) {
      try {
        URL.revokeObjectURL(li._objectUrl);
      } catch (e) {
        /* already revoked or invalid -- nothing to clean up */
      }
      li._objectUrl = null;
    }
  }

  function hideEmptyNotice(gallery) {
    const empty = gallery.querySelector(".fraction-gallery-empty");
    if (empty) empty.hidden = true;
  }

  function errorMessage(error) {
    if (error && error.body && error.body.error) return "Upload failed: " + error.body.error;
    if (error && error.status) return "Upload failed (HTTP " + error.status + ")";
    if (error && error.message) return "Upload failed: " + error.message;
    return "Upload failed";
  }

  // Mutates a pending <li> in place into the same shape buildItem() would have produced for a real,
  // persisted artifact -- this is the temp-id -> real-id reconciliation. Only ever called for a `li`
  // still present in the DOM (the caller already checked); a `li` that isn't found (row removed --
  // cancelled, or the page navigated away and this listener is stale) is handled by the caller instead.
  function reconcileResolved(li, data) {
    li.classList.remove("is-pending");
    li.draggable = true;
    delete li.dataset.tempId;
    li.dataset.id = String(data.id);

    const gallery = galleryOf(li);
    const base = gallery ? gallery.dataset.createUrl : null;
    if (base) {
      li.dataset.updateUrl = base + "/" + data.id;
      li.dataset.deleteUrl = base + "/" + data.id + "/delete";
    }
    li.dataset.rawUrl = data.url || "";

    const link = li.querySelector("a");
    if (link) {
      link.setAttribute("href", data.url || "#");
      link.setAttribute("target", "_blank");
    }
    const img = li.querySelector("img");
    if (img) img.src = data.url || "";
    revokeObjectUrl(li);

    const statusEl = li.querySelector(".fraction-gallery-pending-status");
    if (statusEl) statusEl.remove();
    const toggle = li.querySelector(".fraction-gallery-toggle");
    if (toggle) {
      toggle.hidden = false;
      const checkbox = toggle.querySelector("input");
      if (checkbox) checkbox.checked = true; // matches create_artifact's include_in_report=True default
    }
    const del = li.querySelector(".fraction-gallery-delete");
    if (del) del.title = "Delete artifact";
  }

  function markFailed(li, error) {
    li.classList.remove("is-pending");
    li.classList.add("is-failed");
    const statusEl = li.querySelector(".fraction-gallery-pending-status");
    if (!statusEl) return;
    statusEl.textContent = "";
    statusEl.appendChild(document.createTextNode(errorMessage(error) + " — "));
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "btn fraction-gallery-retry";
    retry.textContent = "Retry";
    statusEl.appendChild(retry);
  }

  // tempId -> { file, caption, createUrl, engagementId, findingId, canceled }. Kept until the upload
  // resolves (success) so a "Retry" click on a failed row can re-enqueue without asking the user to
  // re-pick the file, and so a cancel-while-in-flight can tell the eventual `resolved` handler to
  // clean up the now-orphaned server-side artifact instead of resurrecting a row the user removed.
  const pendingUploads = {};

  // Highest real (persisted) artifact id currently in this gallery. Passed as the outbox dedupe
  // `notBeforeId` so a lost-success retry only treats a row NEWER than everything present at enqueue
  // time as "this upload already landed" -- a pre-existing same-named artifact can't be mistaken for it.
  function maxRealId(gallery) {
    let max = 0;
    const items = gallery.querySelectorAll(".fraction-gallery-item[data-id]");
    for (let i = 0; i < items.length; i++) {
      const n = parseInt(items[i].dataset.id, 10);
      if (!isNaN(n) && n > max) max = n;
    }
    return max;
  }

  function enqueueGalleryUpload(tempId, meta) {
    if (!window.FractionOutbox) return false;
    const filename = meta.file.name || "artifact";
    window.FractionOutbox.enqueueUpload({
      tempId: tempId,
      url: meta.createUrl,
      blob: meta.file,
      filename: filename,
      fields: {
        engagement_id: meta.engagementId || "",
        finding_id: meta.findingId || "",
        caption: meta.caption || "",
      },
      // W1: dedupe on a lost-success retry. listUrl is the finding's artifact list; filename +
      // notBeforeId identify the row this upload created without needing a server idempotency key.
      dedupe: meta.listUrl
        ? { listUrl: meta.listUrl, filename: filename, notBeforeId: meta.notBeforeId }
        : null,
    });
    return true;
  }

  // --- Upload (durable, via the resilience outbox) --------------------------------------------
  // An optimistic preview row is inserted immediately; fraction/static/outbox.js POSTs in the
  // background with retry + exponential backoff and durably survives a reload while offline. This
  // module only reconciles the DOM once the outbox tells it the op resolved or was permanently
  // rejected (docs/_patches/ws14-resilience.md).
  document.addEventListener("submit", function (ev) {
    const form = ev.target.closest(".fraction-gallery-upload");
    if (!form) return;
    ev.preventDefault();

    const gallery = galleryOf(form);
    if (!gallery) return;
    const fileInput = form.querySelector(".fraction-gallery-file");
    const captionInput = form.querySelector(".fraction-gallery-caption-input");
    if (!fileInput || !fileInput.files || !fileInput.files.length) return;

    const file = fileInput.files[0];
    const caption = captionInput ? captionInput.value : "";
    const tempId = "gal-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    const meta = {
      file: file,
      caption: caption,
      createUrl: gallery.dataset.createUrl,
      listUrl: gallery.dataset.listUrl || "",
      notBeforeId: maxRealId(gallery),
      engagementId: form.dataset.engagementId || "",
      findingId: gallery.dataset.findingId || "",
    };
    pendingUploads[tempId] = meta;

    const isImage = file.type && file.type.indexOf("image/") === 0;
    const objectUrl = isImage ? URL.createObjectURL(file) : null;
    const li = buildItem({ filename: file.name, caption: caption }, {
      tempId: tempId,
      objectUrl: objectUrl,
      statusText: "Uploading…",
    });
    if (objectUrl) li._objectUrl = objectUrl;

    const list = gallery.querySelector(".fraction-gallery-list");
    list.appendChild(li);
    hideEmptyNotice(gallery);
    form.reset();

    if (!enqueueGalleryUpload(tempId, meta)) {
      markFailed(li, { message: "resilience outbox unavailable" });
    }
  });

  if (window.FractionOutbox) {
    window.FractionOutbox.on("resolved", function (tempId, data) {
      const meta = pendingUploads[tempId];
      delete pendingUploads[tempId];
      const li = document.querySelector('.fraction-gallery-item[data-temp-id="' + tempId + '"]');
      if (!li) {
        // The row was removed (user cancelled) before the upload finished landing server-side. The
        // outbox has no mid-flight cancellation, so best-effort delete the now-orphaned artifact
        // rather than let it silently reappear the next time the gallery list is refetched.
        if (meta && meta.canceled && data && data.id && meta.createUrl) {
          fetch(meta.createUrl + "/" + data.id + "/delete", { method: "POST" }).catch(function () {});
        }
        return;
      }
      reconcileResolved(li, data);
    });

    window.FractionOutbox.on("failed", function (tempId, error) {
      // Keep pendingUploads[tempId] around -- the Retry button reuses the stashed File. A 4xx never
      // creates a server-side row (validation happens before insert in create_artifact), so there is
      // nothing to clean up here even if the li was already removed.
      const li = document.querySelector('.fraction-gallery-item[data-temp-id="' + tempId + '"]');
      if (!li) return;
      markFailed(li, error);
    });
  }

  document.addEventListener("click", function (ev) {
    const retry = ev.target.closest(".fraction-gallery-retry");
    if (!retry) return;
    const item = retry.closest(".fraction-gallery-item");
    const tempId = item && item.dataset.tempId;
    const meta = tempId && pendingUploads[tempId];
    if (!item || !meta) return;
    item.classList.remove("is-failed");
    item.classList.add("is-pending");
    const statusEl = item.querySelector(".fraction-gallery-pending-status");
    if (statusEl) statusEl.textContent = "Uploading…";
    if (!enqueueGalleryUpload(tempId, meta)) {
      markFailed(item, { message: "resilience outbox unavailable" });
    }
  });

  // --- Include / exclude toggle ----------------------------------------------------------------
  document.addEventListener("change", function (ev) {
    const checkbox = ev.target.closest(".fraction-gallery-include");
    if (!checkbox) return;
    const item = checkbox.closest(".fraction-gallery-item");
    if (!item || !item.dataset.updateUrl) return; // still pending/failed -- no server row to patch yet
    const included = checkbox.checked;
    fetch(item.dataset.updateUrl, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ include_in_report: included }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error("update failed");
        item.classList.toggle("is-excluded", !included);
      })
      .catch(function () {
        checkbox.checked = !included;
        window.alert("Could not update artifact.");
      });
  });

  // --- Caption edit (debounced) -----------------------------------------------------------------
  let captionTimer = null;
  document.addEventListener("input", function (ev) {
    const input = ev.target.closest(".fraction-gallery-caption");
    if (!input) return;
    const item = input.closest(".fraction-gallery-item");
    if (!item || !item.dataset.updateUrl) return; // still pending/failed -- no server row to patch yet
    clearTimeout(captionTimer);
    const value = input.value;
    captionTimer = setTimeout(function () {
      fetch(item.dataset.updateUrl, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ caption: value }),
      }).catch(function () {
        /* best-effort autosave; a visible error on every keystroke would be noisier than useful */
      });
    }, 500);
  });

  // --- Delete (or, for a still-pending/failed row, cancel) ----------------------------------------
  document.addEventListener("click", function (ev) {
    const del = ev.target.closest(".fraction-gallery-delete");
    if (!del) return;
    const item = del.closest(".fraction-gallery-item");
    if (!item) return;

    if (item.dataset.tempId) {
      // No real artifact exists yet (still uploading) or the 4xx-dropped upload never created one --
      // either way there's nothing durable server-side to delete. Just remove the row locally; if the
      // upload was still in flight, mark it cancelled so the `resolved` handler above cleans up the
      // artifact it eventually creates instead of resurrecting a row the user already dismissed.
      if (!window.confirm("Cancel this upload?")) return;
      const meta = pendingUploads[item.dataset.tempId];
      if (meta) meta.canceled = true;
      revokeObjectUrl(item);
      item.remove();
      return;
    }

    if (!window.confirm("Delete this artifact?")) return;
    fetch(item.dataset.deleteUrl, { method: "POST" })
      .then(function (r) {
        if (!r.ok) throw new Error("delete failed");
        item.remove();
      })
      .catch(function () {
        window.alert("Could not delete artifact.");
      });
  });

  // --- Drag-to-reorder (native HTML5 DnD; no external sortable library) --------------------------
  let dragEl = null;

  document.addEventListener("dragstart", function (ev) {
    const item = ev.target.closest(".fraction-gallery-item");
    if (!item) return;
    dragEl = item;
    ev.dataTransfer.effectAllowed = "move";
    try {
      ev.dataTransfer.setData("text/plain", item.dataset.id);
    } catch (e) {
      // Some browsers require setData to be called at all for drag to proceed; ignore failures.
    }
    item.classList.add("is-dragging");
  });

  document.addEventListener("dragend", function (ev) {
    const item = ev.target.closest(".fraction-gallery-item");
    if (item) item.classList.remove("is-dragging");
    dragEl = null;
  });

  document.addEventListener("dragover", function (ev) {
    const over = ev.target.closest(".fraction-gallery-item");
    if (!over || !dragEl || over === dragEl) return;
    ev.preventDefault();
    const list = over.parentElement;
    const rect = over.getBoundingClientRect();
    const before = (ev.clientY - rect.top) / rect.height < 0.5;
    list.insertBefore(dragEl, before ? over : over.nextSibling);
  });

  document.addEventListener("drop", function (ev) {
    const gallery = galleryOf(ev.target);
    if (!gallery || !dragEl) return;
    ev.preventDefault();
    const list = gallery.querySelector(".fraction-gallery-list");
    const order = Array.prototype.map.call(list.children, function (li) {
      return parseInt(li.dataset.id, 10);
    });
    fetch(gallery.dataset.reorderUrl, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ order: order }),
    }).catch(function () {
      window.alert("Could not save the new order.");
    });
  });
})();
