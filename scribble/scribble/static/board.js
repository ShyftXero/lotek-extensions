// Ids are UUIDv7 strings since lotek#335 -- parseInt() returns NaN for one, silently, and the
// request then carries a null/NaN id instead of failing. Ids are opaque here: pass them through.
// scribble/static/board.js — the two-level drag-and-drop finding board (WS3).
//
// Native HTML5 drag-and-drop only (no external/CDN sortable library — same constraint as WS5's
// artifacts.js: Lotek is CSP-strict, no CDN scripts). Two independently draggable levels share one
// event-delegation surface on `document`:
//   - `.scribble-board-group`    (draggable) — reordering these persists via the board's
//                                 `data-reorder-groups-url`; child findings travel with the card as a
//                                 DOM subtree, so they move as one unit for free.
//   - `.scribble-board-finding`  (draggable, nested inside a `.scribble-board-findings` <ul>) —
//                                 dragging one within or across lists persists via that finding's own
//                                 `data-move-url`.
// Because a finding `<li>` is the innermost draggable ancestor of any mousedown inside a group card,
// the browser's native drag-source resolution already keeps the two levels from interfering: starting
// a drag from a finding row drags the row, not its group.
//
// Server semantics this file leans on (see scribble/engagement_ui.py):
//   - POST .../move returns {finding, group, previous_group} so the group pill/re-rank button can be
//     updated in place without a full reload (the destination group flips to order_mode="manual").
//   - POST .../groups/<id> with {order_mode: "auto_severity"} is "re-rank by severity"; the response
//     doesn't include finding order, so that action reloads the page to show the new severity sort.
(function () {
  "use strict";

  if (window.__scribbleBoardInit) return;
  window.__scribbleBoardInit = true;

  function jsonHeaders() {
    return { "Content-Type": "application/json" };
  }

  // The "Ungrouped" bucket has no manual order_mode — it is always rendered severity-first, both on the
  // board and in the report (see engagement_ui.py / reporting/context.py). Reordering WITHIN it would
  // therefore silently revert on the next load, so intra-Ungrouped drags are disabled below. Dragging a
  // finding OUT of Ungrouped into a real group is still allowed (that's a meaningful group_id change).
  function isUngroupedList(list) {
    return !!(list && list.closest(".scribble-board-group-ungrouped"));
  }

  var draggedFinding = null;
  var draggedFindingOriginList = null;
  var draggedGroup = null;

  function applyGroupState(groupId, state) {
    if (groupId == null || !state) return;
    var card = document.querySelector('.scribble-board-group[data-group-id="' + groupId + '"]');
    if (!card) return;
    var pill = card.querySelector(".scribble-board-group-mode");
    var rerankBtn = card.querySelector(".scribble-board-group-rerank");
    if (pill && state.order_mode) pill.textContent = state.order_mode;
    if (rerankBtn) rerankBtn.disabled = state.order_mode === "auto_severity";
  }

  function emptyPlaceholder() {
    var li = document.createElement("li");
    li.className = "scribble-board-findings-empty muted";
    li.textContent = "Drop findings here.";
    return li;
  }

  // --- drag start / end --------------------------------------------------------------------------

  document.addEventListener("dragstart", function (ev) {
    var findingEl = ev.target.closest(".scribble-board-finding");
    if (findingEl) {
      draggedFinding = findingEl;
      draggedFindingOriginList = findingEl.closest(".scribble-board-findings");
      ev.dataTransfer.effectAllowed = "move";
      try {
        ev.dataTransfer.setData("text/plain", findingEl.dataset.findingId || "");
      } catch (e) {
        // Some browsers require setData to be called at all for drag to proceed; ignore failures.
      }
      findingEl.classList.add("is-dragging");
      return;
    }
    var groupEl = ev.target.closest(".scribble-board-group[data-group-id]:not(.scribble-board-group-ungrouped)");
    if (groupEl) {
      draggedGroup = groupEl;
      ev.dataTransfer.effectAllowed = "move";
      try {
        ev.dataTransfer.setData("text/plain", groupEl.dataset.groupId || "");
      } catch (e) {
        // ignore
      }
      groupEl.classList.add("is-dragging");
    }
  });

  document.addEventListener("dragend", function () {
    if (draggedFinding) draggedFinding.classList.remove("is-dragging");
    if (draggedGroup) draggedGroup.classList.remove("is-dragging");
    draggedFinding = null;
    draggedFindingOriginList = null;
    draggedGroup = null;
  });

  // --- drag over: live-move the DOM node so the drop just reads off final positions ---------------

  document.addEventListener("dragover", function (ev) {
    if (draggedFinding) {
      var overFinding = ev.target.closest(".scribble-board-finding");
      var overList = ev.target.closest(".scribble-board-findings");
      // Block intra-Ungrouped reordering (see isUngroupedList): don't let the drag visually rearrange
      // findings that start and stay in the Ungrouped bucket. Dragging one out to a real group list is
      // unaffected (overList would then be that group's list, not the Ungrouped one).
      if (
        overList &&
        isUngroupedList(overList) &&
        draggedFindingOriginList &&
        isUngroupedList(draggedFindingOriginList)
      ) {
        return;
      }
      if (overFinding && overFinding !== draggedFinding) {
        ev.preventDefault();
        var rect = overFinding.getBoundingClientRect();
        var before = (ev.clientY - rect.top) / rect.height < 0.5;
        overFinding.parentElement.insertBefore(draggedFinding, before ? overFinding : overFinding.nextSibling);
      } else if (overList && !overFinding) {
        ev.preventDefault();
        overList.appendChild(draggedFinding);
      }
      return;
    }
    if (draggedGroup) {
      var overGroup = ev.target.closest(
        ".scribble-board-group[data-group-id]:not(.scribble-board-group-ungrouped)"
      );
      var board = ev.target.closest("[data-board]");
      if (overGroup && overGroup !== draggedGroup && board) {
        ev.preventDefault();
        var gRect = overGroup.getBoundingClientRect();
        var beforeGroup = (ev.clientY - gRect.top) / gRect.height < 0.5;
        board.insertBefore(draggedGroup, beforeGroup ? overGroup : overGroup.nextSibling);
      }
    }
  });

  // --- drop: persist the order the dragover pass already arranged in the DOM ----------------------

  document.addEventListener("drop", function (ev) {
    if (draggedFinding) {
      ev.preventDefault();
      var list = draggedFinding.closest(".scribble-board-findings");
      if (!list) return;
      // Safety net mirroring the dragover guard: an intra-Ungrouped move persists nothing (Ungrouped is
      // always severity-sorted), so never POST it — the finding is left exactly where it was.
      if (isUngroupedList(list) && draggedFindingOriginList && isUngroupedList(draggedFindingOriginList)) {
        return;
      }
      var order = Array.prototype.filter
        .call(list.children, function (li) {
          return li.classList.contains("scribble-board-finding");
        })
        .map(function (li) {
          return li.dataset.findingId;
        });
      var index = order.indexOf(draggedFinding.dataset.findingId);
      var groupId = list.dataset.groupId ? list.dataset.groupId : null;
      var moveUrl = draggedFinding.dataset.moveUrl;
      var originList = draggedFindingOriginList;

      fetch(moveUrl, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ group_id: groupId, order_index: index }),
      })
        .then(function (r) {
          if (!r.ok) throw new Error("move failed");
          return r.json();
        })
        .then(function (data) {
          var placeholder = list.querySelector(".scribble-board-findings-empty");
          if (placeholder) placeholder.remove();
          if (originList && originList !== list) {
            var stillHasFindings = Array.prototype.some.call(originList.children, function (li) {
              return li.classList.contains("scribble-board-finding");
            });
            if (!stillHasFindings && !originList.querySelector(".scribble-board-findings-empty")) {
              originList.appendChild(emptyPlaceholder());
            }
          }
          if (data.group) applyGroupState(data.group.id, data.group);
          if (data.previous_group) applyGroupState(data.previous_group.id, data.previous_group);
        })
        .catch(function () {
          window.alert("Could not move the finding. Reloading to resync.");
          window.location.reload();
        });
      return;
    }

    if (draggedGroup) {
      ev.preventDefault();
      var board = draggedGroup.closest("[data-board]");
      if (!board) return;
      var groupOrder = Array.prototype.filter
        .call(board.children, function (el) {
          return el.classList.contains("scribble-board-group") && el.dataset.groupId;
        })
        .map(function (el) {
          return el.dataset.groupId;
        });
      fetch(board.dataset.reorderGroupsUrl, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ order: groupOrder }),
      }).catch(function () {
        window.alert("Could not save the new section order. Reloading to resync.");
        window.location.reload();
      });
    }
  });

  // --- group inline controls: rename / include-toggle / re-rank -----------------------------------

  document.addEventListener("change", function (ev) {
    var nameInput = ev.target.closest(".scribble-board-group-name");
    if (nameInput) {
      var card = nameInput.closest(".scribble-board-group");
      var value = nameInput.value.trim();
      if (!card || !value) return;
      fetch(card.dataset.updateUrl, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ name: value }),
      }).catch(function () {
        window.alert("Could not rename the group.");
      });
      return;
    }
    var includeToggle = ev.target.closest(".scribble-board-group-include");
    if (includeToggle) {
      var card2 = includeToggle.closest(".scribble-board-group");
      if (!card2) return;
      fetch(card2.dataset.updateUrl, {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ include_in_report: includeToggle.checked }),
      }).catch(function () {
        includeToggle.checked = !includeToggle.checked;
        window.alert("Could not update the group.");
      });
    }
  });

  // --- multi-select bulk move (ext#143) ----------------------------------------------------------
  // Checkboxes on finding rows drive a bulk bar; "Move selected" posts every checked id to the board's
  // data-batch-move-url in ONE atomic request (the singular drag path is untouched).

  function checkedFindingIds() {
    return Array.prototype.slice
      .call(document.querySelectorAll(".scribble-finding-check:checked"))
      .map(function (cb) {
        var li = cb.closest(".scribble-board-finding");
        return li ? li.dataset.findingId : null;
      })
      .filter(Boolean);
  }

  function updateBulkBar() {
    var bar = document.getElementById("scribble-bulk-bar");
    if (!bar) return;
    var ids = checkedFindingIds();
    var n = document.getElementById("scribble-bulk-n");
    if (n) n.textContent = String(ids.length);
    bar.hidden = ids.length === 0;
  }

  document.addEventListener("change", function (ev) {
    if (ev.target.closest(".scribble-finding-check")) updateBulkBar();
  });

  document.addEventListener("click", function (ev) {
    if (ev.target.closest("#scribble-bulk-clear")) {
      Array.prototype.forEach.call(
        document.querySelectorAll(".scribble-finding-check:checked"),
        function (cb) { cb.checked = false; }
      );
      updateBulkBar();
      return;
    }
    if (!ev.target.closest("#scribble-bulk-move")) return;
    var board = document.querySelector("[data-batch-move-url]");
    var groupSel = document.getElementById("scribble-bulk-group");
    if (!board || !groupSel) return;
    var ids = checkedFindingIds();
    if (!ids.length) return;
    var groupId = groupSel.value ? groupSel.value : null;  // "" -> Ungrouped (null)
    var label = groupSel.options[groupSel.selectedIndex].textContent.trim();
    if (!window.confirm("Move " + ids.length + " finding(s) to \"" + label + "\"?")) return;
    fetch(board.dataset.batchMoveUrl, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ finding_ids: ids, group_id: groupId, order_index: 0 }),
    })
      .then(function (r) { if (!r.ok) throw new Error("bulk move failed"); return r.json(); })
      .then(function () { window.location.reload(); })
      .catch(function () {
        window.alert("Could not move the selected findings. Reloading to resync.");
        window.location.reload();
      });
  });

  document.addEventListener("click", function (ev) {
    var rerankBtn = ev.target.closest(".scribble-board-group-rerank");
    if (!rerankBtn || rerankBtn.disabled) return;
    var card = rerankBtn.closest(".scribble-board-group");
    if (!card) return;
    fetch(card.dataset.updateUrl, {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ order_mode: "auto_severity" }),
    })
      .then(function (r) {
        if (!r.ok) throw new Error("re-rank failed");
        return r.json();
      })
      .then(function () {
        // Findings re-sort worst-first on the server; reload rather than reimplementing the
        // severity comparator in JS.
        window.location.reload();
      })
      .catch(function () {
        window.alert("Could not re-rank the group.");
      });
  });

  // --- attack-path linking (ext#141) --------------------------------------------------------------
  // Scribble has no seam to vector, so the picker talks to vector's cookie API directly (same origin):
  // list the author's diagrams, fetch the chosen one's self-contained export.html, and POST that
  // snapshot to scribble's cookie link route. Vector's cookie routes are tenancy-scoped, so a diagram
  // the author cannot see is never offered.
  (function initAttackPaths() {
    var section = document.querySelector(".scribble-attack-paths[data-link-url]");
    if (!section) return;
    var select = document.getElementById("scribble-diagram-select");
    var linkBtn = document.getElementById("scribble-diagram-link-btn");
    var captionEl = document.getElementById("scribble-diagram-caption");
    var msg = document.getElementById("scribble-diagram-link-msg");
    if (!select || !linkBtn) return;
    var vectorBase = (section.dataset.vectorBase || "/vector").replace(/\/$/, "");

    function setMsg(t) { if (msg) msg.textContent = t || ""; }

    fetch(vectorBase + "/api/diagrams", { headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("vector unavailable"); return r.json(); })
      .then(function (data) {
        var diagrams = (data && data.diagrams) || [];
        select.innerHTML = "";
        if (!diagrams.length) {
          select.innerHTML = '<option value="">No Vector diagrams available</option>';
          linkBtn.disabled = true;
          return;
        }
        select.appendChild(new Option("Choose a diagram…", ""));
        diagrams.forEach(function (d) { select.appendChild(new Option(d.name, d.id)); });
      })
      .catch(function () {
        select.innerHTML = '<option value="">Vector not available</option>';
        linkBtn.disabled = true;
      });

    linkBtn.addEventListener("click", function () {
      var diagramId = select.value;
      if (!diagramId) { setMsg("Pick a diagram first."); return; }
      linkBtn.disabled = true;
      setMsg("Fetching diagram…");
      fetch(vectorBase + "/diagrams/" + encodeURIComponent(diagramId) + "/export.html")
        .then(function (r) { if (!r.ok) throw new Error("export failed"); return r.text(); })
        .then(function (embedHtml) {
          setMsg("Linking…");
          return fetch(section.dataset.linkUrl, {
            method: "POST",
            headers: jsonHeaders(),
            body: JSON.stringify({
              diagram_ref: diagramId,
              caption: (captionEl && captionEl.value.trim()) || null,
              embed_html: embedHtml,
            }),
          });
        })
        .then(function (r) { if (!r.ok) throw new Error("link failed"); return r.json(); })
        .then(function () { window.location.reload(); })
        .catch(function () { setMsg("Could not link that diagram."); linkBtn.disabled = false; });
    });
  })();

  // --- adopt a scan job (#630) ---------------------------------------------------------------------
  // The Source-jobs panel picker: POST the entered job id to the adopt route, which LINKS it into this
  // engagement (refuse-on-conflict) and pours its findings onto the board. A 409 means the job is
  // already adopted elsewhere — surface it, never swallow it. No host hook lists promotable jobs, so
  // the operator supplies the id (the panel's follow-on `<select>` waits on such a hook).
  (function initAdoptJob() {
    var panel = document.querySelector(".scribble-source-jobs[data-adopt-url]");
    if (!panel) return;
    var input = document.getElementById("scribble-adopt-job-id");
    var btn = document.getElementById("scribble-adopt-job-btn");
    var msg = document.getElementById("scribble-adopt-job-msg");
    if (!input || !btn) return;

    function setMsg(t) { if (msg) msg.textContent = t || ""; }

    btn.addEventListener("click", function () {
      var jobId = (input.value || "").trim();
      if (!jobId) { setMsg("Enter a scan job id first."); return; }
      btn.disabled = true;
      setMsg("Adopting…");
      fetch(panel.dataset.adoptUrl.replace("__JOBID__", encodeURIComponent(jobId)), { method: "POST" })
        .then(function (r) {
          if (r.status === 409) { setMsg("That job is already adopted by another engagement."); return; }
          if (r.status === 404) { setMsg("No such scan job, or you can't see it."); return; }
          if (!r.ok && r.status !== 302) { throw new Error("adopt failed"); }
          window.location.reload();
        })
        .catch(function () { setMsg("Could not adopt that job."); })
        .finally(function () { btn.disabled = false; });
    });
  })();

  // --- destructive un-adopt (#635) ---------------------------------------------------------------
  // The link-only un-adopt is a plain form POST (no JS). The DESTRUCTIVE path must show the operator
  // EXACTLY which findings it will delete before it acts, so it previews first (the server's preview
  // route returns the same set the destroy route removes), confirms, then POSTs the destroy.
  (function initDestructiveUnadopt() {
    document.querySelectorAll(".scribble-unadopt-destroy").forEach(function (btn) {
      btn.addEventListener("click", function () {
        btn.disabled = true;
        fetch(btn.dataset.previewUrl)
          .then(function (r) { if (!r.ok) throw new Error("preview failed"); return r.json(); })
          .then(function (data) {
            var findings = data.findings || [];
            if (!findings.length) {
              alert("This job enriched no findings still on the board — nothing to delete. Use "
                + "“Un-adopt (keep findings)” to drop the link.");
              return;
            }
            var titles = findings.map(function (f) { return "  • " + f.title; }).join("\n");
            if (!window.confirm("Delete " + findings.length + " finding(s) this job enriched?\n\n"
                + titles + "\n\nThis cannot be undone.")) return;
            var form = document.createElement("form");
            form.method = "post";
            form.action = btn.dataset.destroyUrl;
            document.body.appendChild(form);
            form.submit();
          })
          .catch(function () { alert("Could not preview which findings would be removed."); })
          .finally(function () { btn.disabled = false; });
      });
    });
  })();
})();
