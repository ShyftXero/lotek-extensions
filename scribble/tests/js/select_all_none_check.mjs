// Self-check for the shared Select all / Select none control (scribble/static/select-all.js). Run:
//   node tests/js/select_all_none_check.mjs
// (tests/test_select_all_none_ui.py shells out to this, so it also runs under pytest.)
//
// The helper talks to a real DOM through `.click()`, and this host has no browser (denied list). So it
// is driven here by a small stub: one checkbox factory that reproduces the two browser behaviours the
// helper depends on — `.click()` flips `checked` AND fires a `change` event, and a disabled checkbox
// does not toggle at all. Everything else the helper touches (addEventListener, setAttribute,
// indeterminate) is plain property/method state.
//
// What this pins is the defect the control exists to fix: a row box is never changed by assigning
// `.checked`, so the page's own listener ALWAYS runs and the bulk bar can never show "0 selected"
// over 40 ticked rows. Plus the three-way state, the no-double-toggle rule, the disabled-row rule,
// and that "select none" clears rows the current filter has hidden.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const HELPER = join(HERE, "..", "..", "scribble", "static", "select-all.js");

// ── Stub DOM ─────────────────────────────────────────────────────────────────
// The helper only reaches the DOM through document.getElementById (for string ids) and the elements
// it is handed. Keep the stub at exactly that surface so it cannot drift into pretending to be a DOM.
const byId = new Map();
globalThis.document = {
  getElementById: (id) => byId.get(id) || null,
};

function mount(el, id) {
  el.id = id;
  el.attrs = {};
  el.listeners = { change: [], click: [] };
  el.clicks = 0;
  el.fired = 0;                                 // change events this box actually dispatched
  el.addEventListener = (type, fn) => { (el.listeners[type] ||= []).push(fn); };
  el.setAttribute = (k, v) => { el.attrs[k] = String(v); };
  el.getAttribute = (k) => (k in el.attrs ? el.attrs[k] : null);
  byId.set(id, el);
  return el;
}

function checkbox(id, { checked = false, disabled = false } = {}) {
  const el = mount({ tag: "input", type: "checkbox", checked, disabled, indeterminate: false }, id);
  el.click = () => {
    el.clicks++;
    if (el.disabled) return;              // a disabled box does not toggle on click
    el.checked = !el.checked;
    el.indeterminate = false;             // a real click clears the mixed flag
    el.fired++;
    for (const fn of el.listeners.change) fn({ type: "change", target: el });
  };
  // What the operator does with a mouse; also what the page's own restore path would fire.
  el.userToggle = () => {
    el.checked = !el.checked;
    el.fired++;
    for (const fn of el.listeners.change) fn({ type: "change", target: el });
  };
  return el;
}

function button(id) {
  const el = mount({ tag: "button", disabled: false }, id);
  el.click = () => { for (const fn of el.listeners.click) fn({ type: "click", target: el }); };
  return el;
}

// Load the helper: it assigns window.LotekSelectAll on `window`.
globalThis.window = globalThis;
new Function(readFileSync(HELPER, "utf8"))();

// The page side: one listener per row box, exactly like assessments.html / job.html.
function page({ visible, hidden = [] }) {
  const master = checkbox("selectAll");
  const none = button("selectNone");
  const seen = [];                              // every change the page observes, in order
  const rows = () => visible;
  const allRows = () => visible.concat(hidden);
  const ctl = globalThis.LotekSelectAll({
    master: "selectAll",
    none: "selectNone",
    rows,
    allRows,
    onAfterBatch: (n) => seen.push(`batch:${n}`),
  });
  const wire = (cb) => cb.addEventListener("change", () => {
    if (ctl.isApplying()) return;               // a bulk apply reports once, via onAfterBatch
    seen.push(cb.id);
    ctl.sync();
  });
  visible.concat(hidden).forEach(wire);
  return { master, none, ctl, seen, visible, hidden, allRows };
}

// ── 1. Select all ticks every enabled row through the page's own listener ─────
{
  const p = page({
    visible: [checkbox("a"), checkbox("b"), checkbox("c", { disabled: true })],
    hidden: [checkbox("h1")],
  });
  p.master.checked = true;                      // what the browser does before `change` fires
  p.master.listeners.change.forEach((fn) => fn({ type: "change", target: p.master }));

  assert.deepEqual(p.visible.map((c) => c.checked), [true, true, false], "disabled row must stay unselected");
  assert.equal(p.hidden[0].checked, false, "select all covers the visible set only");
  // The decisive half: every box got a real click, so every listener the page attached ran. A
  // `.checked = true` batch would leave these at 0 and the page's bulk bar would still read 0.
  assert.deepEqual(p.visible.map((c) => c.fired), [1, 1, 0], "each enabled row fired its own change event");
  assert.deepEqual(p.seen, ["batch:2"], "page bookkeeping settles once, not once per row");
  assert.equal(p.master.checked, true);
  assert.equal(p.master.indeterminate, false);
  assert.equal(p.master.getAttribute("aria-checked"), "true");
  assert.equal(p.none.disabled, false, "Select none is live once something is selected");
}

// ── 2. Pressing it again never double-toggles ────────────────────────────────
{
  const p = page({ visible: [checkbox("a"), checkbox("b")] });
  p.master.checked = true;
  p.master.listeners.change.forEach((fn) => fn({ type: "change", target: p.master }));
  const before = p.visible.map((c) => c.clicks);
  p.none.click();                                // clear...
  p.visible.forEach((c) => { c.checked = true; }); // ...then force the "already correct" state
  const changed = p.ctl.apply(true);
  assert.equal(changed, 0, "no box is clicked when it already holds the wanted state");
  assert.deepEqual(p.visible.map((c) => c.clicks), before.map((n) => n + 1), "only the none-click touched them");
  assert.deepEqual(p.visible.map((c) => c.checked), [true, true], "still selected, not toggled back off");
}

// ── 3. Select none clears hidden rows too ────────────────────────────────────
{
  const p = page({ visible: [checkbox("a", { checked: true })], hidden: [checkbox("h1", { checked: true })] });
  p.ctl.sync();
  p.master.checked = false;                      // the browser's state on click
  p.none.click();
  assert.equal(p.visible[0].checked, false, "visible row cleared");
  assert.equal(p.hidden[0].checked, false, "filtered-out row cleared as well — no phantom selection left");
  assert.equal(p.master.getAttribute("aria-checked"), "false");
  assert.equal(p.master.disabled, false, "rows exist, so the master stays usable");
  assert.equal(p.none.disabled, true, "nothing selected: Select none is inert");
}

// ── 3b. Filtered to empty with a hidden selection: Select none must still work ─
{
  const p = page({ visible: [], hidden: [checkbox("h1", { checked: true })] });
  p.ctl.sync();
  assert.equal(p.master.disabled, true, "no visible row means nothing for Select all to do");
  assert.equal(p.none.disabled, false, "but a hidden selection must still be clearable");
  p.none.click();
  assert.equal(p.hidden[0].checked, false);
  assert.equal(p.none.disabled, true);
}

// ── 4. Partial selection is mixed, and a row toggle moves the tri-state ──────
{
  const p = page({ visible: [checkbox("a"), checkbox("b"), checkbox("c")] });
  p.visible[0].userToggle();
  assert.equal(p.master.checked, false);
  assert.equal(p.master.indeterminate, true, "some but not all selected");
  assert.equal(p.master.getAttribute("aria-checked"), "mixed", "screen readers get the mixed state");

  p.visible[1].userToggle();
  p.visible[2].userToggle();
  assert.equal(p.master.checked, true);
  assert.equal(p.master.indeterminate, false);
  assert.equal(p.master.getAttribute("aria-checked"), "true");

  p.visible[2].userToggle();
  assert.equal(p.master.indeterminate, true, "a per-row deselect returns the master to mixed");
}

// ── 5. An empty or fully disabled table parks the control ────────────────────
{
  const p = page({ visible: [checkbox("a", { disabled: true })] });
  assert.equal(p.master.disabled, true, "no selectable row: nothing to select");
  assert.equal(p.master.getAttribute("aria-checked"), "false");
  assert.equal(p.ctl.apply(true), 0);
  assert.equal(p.visible[0].clicks, 0, "a disabled row is never clicked");
}

// ── 6. Two masters, one selection (the job page shows the same state twice) ──
{
  const second = checkbox("selectAll2");
  const none2 = button("selectNone2");
  const rows = [checkbox("a"), checkbox("b")];
  const ctl = globalThis.LotekSelectAll({
    master: ["selectAll", "selectAll2"],
    none: ["selectNone", "selectNone2"],
    rows: () => rows,
  });
  rows[0].addEventListener("change", () => ctl.sync());
  rows[0].userToggle();
  assert.equal(second.indeterminate, true, "the second master mirrors the first");
  assert.equal(second.getAttribute("aria-checked"), "mixed");
  none2.click();
  assert.equal(rows[0].checked, false, "either Select none clears the one shared selection");
  assert.equal(second.checked, false);
}

// ── 7. The "N of M selected" chip is written on every change (it is a live region) ──
{
  const chip = mount({ tag: "span", textContent: "0 of 0 selected" }, "selectCount");
  const rows = [checkbox("a"), checkbox("b"), checkbox("c")];
  const ctl = globalThis.LotekSelectAll({
    master: "selectAll",
    none: "selectNone",
    count: "selectCount",
    rows: () => rows,
  });
  rows.forEach((r) => r.addEventListener("change", () => { if (!ctl.isApplying()) ctl.sync(); }));
  // Written at load: an unwritten live region shows "0 of 0" over a board of 3 and announces nothing.
  assert.equal(chip.textContent, "0 of 3 selected");
  rows[0].userToggle();
  assert.equal(chip.textContent, "1 of 3 selected", "one tick moves the count");
  ctl.apply(true);
  assert.equal(chip.textContent, "3 of 3 selected", "select all reports N of N");
  ctl.apply(false);
  assert.equal(chip.textContent, "0 of 3 selected", "select none returns to 0 of N");
}

console.log("select_all_none_check: all assertions passed");
