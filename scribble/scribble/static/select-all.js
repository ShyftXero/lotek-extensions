// select-all.js — the tri-state "Select all / Select none" control for the findings board.
//
// Mirrors core's src/app/static/select-all.js: the two repositories cannot share code and scribble may
// not import lotek, so the file is carried here verbatim. Keep the two in step — an operator meets the
// same control on core's scan list and on this board.
//
// The bug this exists to prevent: assigning `.checked = true` on the row boxes changes the pixels but
// fires no `change` listener, so the bulk bar (board.js updateBulkBar) never updates and the operator
// ticks 40 findings and reads "0 selected". So a row box is NEVER assigned here. It is clicked, which
// is what a mouse click does: the box flips AND its listeners run. Boxes already in the wanted state
// are skipped, so a box never double-toggles.
//
// Contract (all options optional except `rows`):
//   master   — <input type="checkbox">, or an array of them (element or id). Several pages show the
//              same selection in two bars; hand them all in and they move together.
//   none     — <button>, or an array of them, that clears the selection. Disabled when nothing is
//              selected (an enabled "Select none" on an empty table reads as a dead control).
//   rows     — function returning the row checkboxes that "select all" applies to. Pass a live getter,
//              not a snapshot, so a re-rendered table is still covered.
//   allRows  — function returning every row checkbox, including ones a filter currently hides.
//              Used only by "select none": clearing must not leave a hidden selection behind, or the
//              count and the bulk POST disagree. Defaults to `rows`.
//   count    — element (or id) that displays "N of M selected", kept in step on every change. Put
//              role="status" aria-live="polite" on it and the count announces itself; a live region
//              that is never written is worse than none, because it looks like the feature works.
//   onAfterBatch — called once after a bulk apply with the number of boxes that changed. Row-level
//              listeners should do nothing while `isApplying()` is true and let this fire once.
//
// Accessibility: `aria-checked` is set to true/false/mixed on the master, which is the state screen
// readers announce for a partial selection. A native checkbox's `indeterminate` flag alone is not
// announced by every screen reader, so both are set.
window.LotekSelectAll = function (opts) {
  "use strict";
  opts = opts || {};

  function list(v) {
    if (v == null) return [];
    if (typeof v === "string") v = [v];
    if (!Array.isArray(v)) v = [v];
    return v
      .map(function (m) { return typeof m === "string" ? document.getElementById(m) : m; })
      .filter(Boolean);
  }

  var masters = list(opts.master);
  var noneBtns = list(opts.none);
  var countEls = list(opts.count);
  var rowsFn = opts.rows || function () { return []; };
  var allRowsFn = opts.allRows || rowsFn;
  var applying = false;

  function enabled(fn) {
    var boxes = fn() || [];
    return Array.prototype.slice.call(boxes).filter(function (cb) {
      return cb && !cb.disabled;  // a disabled row is never selected, by us or by the operator
    });
  }

  function sync() {
    var boxes = enabled(rowsFn);
    var total = boxes.length;
    var n = 0;
    boxes.forEach(function (cb) { if (cb.checked) n++; });
    var state = total === 0 || n === 0 ? "false" : n === total ? "true" : "mixed";
    masters.forEach(function (m) {
      m.checked = state === "true";
      m.indeterminate = state === "mixed";
      m.setAttribute("aria-checked", state);
      // Nothing visible to select (or the filter hides every row): the master has no work to do.
      m.disabled = total === 0;
    });
    // Select none clears what a filter hides too, so it stays live while ANY row is selected --
    // otherwise a filtered-to-empty table would strand a selection with no control able to clear it.
    var any = enabled(allRowsFn).some(function (cb) { return cb.checked; });
    noneBtns.forEach(function (b) { b.disabled = !any; });
    countEls.forEach(function (c) { c.textContent = n + " of " + total + " selected"; });
    return n;
  }

  function apply(state) {
    var boxes = enabled(state ? rowsFn : allRowsFn);
    var changed = 0;
    applying = true;
    try {
      boxes.forEach(function (cb) {
        if (cb.checked === state) return;  // already correct: clicking would toggle it the wrong way
        cb.click();                        // real click: flips the box AND runs its listeners
        changed++;
      });
    } finally {
      applying = false;
    }
    sync();
    if (opts.onAfterBatch) opts.onAfterBatch(changed);
    return changed;
  }

  masters.forEach(function (m) {
    m.addEventListener("change", function () {
      // A click on an indeterminate master lands here with checked === true (the browser clears the
      // mixed flag first), so this reads the operator's intent, not the stale tri-state.
      apply(m.checked);
    });
  });
  noneBtns.forEach(function (b) {
    b.addEventListener("click", function () { apply(false); });
  });
  sync();

  return {
    sync: sync,
    apply: apply,
    isApplying: function () { return applying; },
  };
};
