// fraction/static/board.js — the two-level drag-and-drop finding board (WS3).
//
// Native HTML5 drag-and-drop only (no external/CDN sortable library — same constraint as WS5's
// artifacts.js: Lotek is CSP-strict, no CDN scripts). Two independently draggable levels share one
// event-delegation surface on `document`:
//   - `.fraction-board-group`    (draggable) — reordering these persists via the board's
//                                 `data-reorder-groups-url`; child findings travel with the card as a
//                                 DOM subtree, so they move as one unit for free.
//   - `.fraction-board-finding`  (draggable, nested inside a `.fraction-board-findings` <ul>) —
//                                 dragging one within or across lists persists via that finding's own
//                                 `data-move-url`.
// Because a finding `<li>` is the innermost draggable ancestor of any mousedown inside a group card,
// the browser's native drag-source resolution already keeps the two levels from interfering: starting
// a drag from a finding row drags the row, not its group.
//
// Server semantics this file leans on (see fraction/engagement_ui.py):
//   - POST .../move returns {finding, group, previous_group} so the group pill/re-rank button can be
//     updated in place without a full reload (the destination group flips to order_mode="manual").
//   - POST .../groups/<id> with {order_mode: "auto_severity"} is "re-rank by severity"; the response
//     doesn't include finding order, so that action reloads the page to show the new severity sort.
(function () {
  "use strict";

  if (window.__fractionBoardInit) return;
  window.__fractionBoardInit = true;

  function jsonHeaders() {
    return { "Content-Type": "application/json" };
  }

  // The "Ungrouped" bucket has no manual order_mode — it is always rendered severity-first, both on the
  // board and in the report (see engagement_ui.py / reporting/context.py). Reordering WITHIN it would
  // therefore silently revert on the next load, so intra-Ungrouped drags are disabled below. Dragging a
  // finding OUT of Ungrouped into a real group is still allowed (that's a meaningful group_id change).
  function isUngroupedList(list) {
    return !!(list && list.closest(".fraction-board-group-ungrouped"));
  }

  var draggedFinding = null;
  var draggedFindingOriginList = null;
  var draggedGroup = null;

  function applyGroupState(groupId, state) {
    if (groupId == null || !state) return;
    var card = document.querySelector('.fraction-board-group[data-group-id="' + groupId + '"]');
    if (!card) return;
    var pill = card.querySelector(".fraction-board-group-mode");
    var rerankBtn = card.querySelector(".fraction-board-group-rerank");
    if (pill && state.order_mode) pill.textContent = state.order_mode;
    if (rerankBtn) rerankBtn.disabled = state.order_mode === "auto_severity";
  }

  function emptyPlaceholder() {
    var li = document.createElement("li");
    li.className = "fraction-board-findings-empty muted";
    li.textContent = "Drop findings here.";
    return li;
  }

  // --- drag start / end --------------------------------------------------------------------------

  document.addEventListener("dragstart", function (ev) {
    var findingEl = ev.target.closest(".fraction-board-finding");
    if (findingEl) {
      draggedFinding = findingEl;
      draggedFindingOriginList = findingEl.closest(".fraction-board-findings");
      ev.dataTransfer.effectAllowed = "move";
      try {
        ev.dataTransfer.setData("text/plain", findingEl.dataset.findingId || "");
      } catch (e) {
        // Some browsers require setData to be called at all for drag to proceed; ignore failures.
      }
      findingEl.classList.add("is-dragging");
      return;
    }
    var groupEl = ev.target.closest(".fraction-board-group[data-group-id]:not(.fraction-board-group-ungrouped)");
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
      var overFinding = ev.target.closest(".fraction-board-finding");
      var overList = ev.target.closest(".fraction-board-findings");
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
        ".fraction-board-group[data-group-id]:not(.fraction-board-group-ungrouped)"
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
      var list = draggedFinding.closest(".fraction-board-findings");
      if (!list) return;
      // Safety net mirroring the dragover guard: an intra-Ungrouped move persists nothing (Ungrouped is
      // always severity-sorted), so never POST it — the finding is left exactly where it was.
      if (isUngroupedList(list) && draggedFindingOriginList && isUngroupedList(draggedFindingOriginList)) {
        return;
      }
      var order = Array.prototype.filter
        .call(list.children, function (li) {
          return li.classList.contains("fraction-board-finding");
        })
        .map(function (li) {
          return li.dataset.findingId;
        });
      var index = order.indexOf(draggedFinding.dataset.findingId);
      var groupId = list.dataset.groupId ? parseInt(list.dataset.groupId, 10) : null;
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
          var placeholder = list.querySelector(".fraction-board-findings-empty");
          if (placeholder) placeholder.remove();
          if (originList && originList !== list) {
            var stillHasFindings = Array.prototype.some.call(originList.children, function (li) {
              return li.classList.contains("fraction-board-finding");
            });
            if (!stillHasFindings && !originList.querySelector(".fraction-board-findings-empty")) {
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
          return el.classList.contains("fraction-board-group") && el.dataset.groupId;
        })
        .map(function (el) {
          return parseInt(el.dataset.groupId, 10);
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
    var nameInput = ev.target.closest(".fraction-board-group-name");
    if (nameInput) {
      var card = nameInput.closest(".fraction-board-group");
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
    var includeToggle = ev.target.closest(".fraction-board-group-include");
    if (includeToggle) {
      var card2 = includeToggle.closest(".fraction-board-group");
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

  document.addEventListener("click", function (ev) {
    var rerankBtn = ev.target.closest(".fraction-board-group-rerank");
    if (!rerankBtn || rerankBtn.disabled) return;
    var card = rerankBtn.closest(".fraction-board-group");
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
})();
