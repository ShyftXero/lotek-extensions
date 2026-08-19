/* Engagement coverage panel: drives the checklist JSON API (assign, per-item status/note, report
 * toggle, remove). Non-blocking visual reminders; nothing here gates anything. */
(function () {
  "use strict";
  var panel = document.getElementById("checklist-panel");
  if (!panel) return;
  var eid = panel.dataset.engagementId;
  var scope = panel.dataset.scope || "";
  var API = (panel.dataset.templatesUrl || "").replace(/\/checklists\/templates$/, "");
  var body = document.getElementById("ckp-body");
  var tray = document.getElementById("ckp-assign-tray");

  var BUCKETS = [["satisfied", "Satisfied"], ["deficient", "Deficient"],
                 ["not_applicable", "N/A"], ["open", "Open"]];

  function jget(url) { return fetch(url).then(function (r) { return r.json(); }); }
  function jpost(url, data) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data || {})
    }).then(function (r) { return r.json(); });
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function rollupBar(rollup) {
    var wrap = el("span", "ckp-rollup");
    BUCKETS.forEach(function (b) {
      var n = (rollup && rollup[b[0]]) || 0;
      if (n) wrap.appendChild(el("span", "ckp-chip ck-" + b[0], b[1] + ": " + n));
    });
    return wrap;
  }

  function statusSelect(cl, item) {
    var sel = el("select", "ckp-status");
    var opts = (cl.recommended_status || []).slice();
    if (item.status && opts.indexOf(item.status) === -1) opts.push(item.status);
    opts.forEach(function (s) {
      var o = el("option", null, s);
      o.value = s;
      if (s === item.status) o.selected = true;
      sel.appendChild(o);
    });
    var custom = el("option", null, "custom…");
    custom.value = "__custom__";
    sel.appendChild(custom);
    sel.addEventListener("change", function () {
      var v = sel.value;
      if (v === "__custom__") {
        v = window.prompt("Custom status:", item.status || "");
        if (!v) { load(); return; }
      }
      jpost(API + "/engagement-checklist-items/" + item.id, { status: v }).then(load);
    });
    return sel;
  }

  function renderChecklist(cl) {
    var card = el("div", "ckp-list");
    var head = el("div", "ckp-list-head");
    head.appendChild(el("strong", null, cl.name));
    head.appendChild(el("span", "ckp-kind", cl.kind));
    head.appendChild(rollupBar(cl.rollup));

    var lbl = el("label", "ckp-toggle");
    var cb = el("input");
    cb.type = "checkbox";
    cb.checked = !!cl.include_in_report;
    cb.addEventListener("change", function () {
      jpost(API + "/engagement-checklists/" + cl.id, { include_in_report: cb.checked });
    });
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" in report"));
    head.appendChild(lbl);

    var rm = el("button", "btn ghost ckp-remove", "remove");
    rm.addEventListener("click", function () {
      if (window.confirm("Remove this checklist from the engagement?")) {
        jpost(API + "/engagement-checklists/" + cl.id + "/delete").then(load);
      }
    });
    head.appendChild(rm);
    card.appendChild(head);

    var lastSection;
    (cl.items || []).forEach(function (item) {
      if (item.section !== lastSection) {
        lastSection = item.section;
        if (item.section) card.appendChild(el("div", "ckp-section", item.section));
      }
      var row = el("div", "ckp-item");
      row.appendChild(statusSelect(cl, item));
      var mid = el("div", "ckp-item-mid");
      var t = el("div", "ckp-item-text", item.text);
      if (item.control_ref) {
        t.appendChild(el("span", "ckp-ctrl",
          " [" + (item.framework ? item.framework + " " : "") + item.control_ref + "]"));
      }
      mid.appendChild(t);
      var note = el("input", "ckp-note");
      note.type = "text";
      note.placeholder = "note";
      note.value = item.note || "";
      note.addEventListener("change", function () {
        jpost(API + "/engagement-checklist-items/" + item.id, { note: note.value });
      });
      mid.appendChild(note);
      row.appendChild(mid);
      card.appendChild(row);
    });
    return card;
  }

  function render(checklists) {
    body.innerHTML = "";
    if (!checklists || !checklists.length) {
      body.appendChild(el("p", "muted", "No checklists assigned yet."));
      return;
    }
    checklists.forEach(function (cl) { body.appendChild(renderChecklist(cl)); });
  }

  function load() {
    jget(API + "/engagements/" + eid + "/checklists").then(function (d) {
      render((d && d.checklists) || []);
    });
  }

  document.getElementById("ckp-assign-btn").addEventListener("click", function () {
    if (!tray.hidden) { tray.hidden = true; return; }
    jget(API + "/checklists/templates/suggest?category=" + encodeURIComponent(scope)).then(function (d) {
      tray.innerHTML = "";
      [["Suggested", d.suggested], ["All", d.others]].forEach(function (g) {
        if (!g[1] || !g[1].length) return;
        tray.appendChild(el("div", "ckp-tray-h", g[0]));
        g[1].forEach(function (t) {
          // `ckp-tmpl` dropped: no stylesheet ever defined it, so it read as styling intent that does
          // not exist. `.btn` carries the whole look of these buttons (ext#44). `ghost` stays as-is
          // here and on `.ckp-remove` — it is also undefined in scribble/lotek's shared CSS, but that
          // is one class across two panels and belongs in its own change, not this one.
          var b = el("button", "btn ghost",
            t.name + " (" + t.kind + ", " + t.item_count + ")");
          b.addEventListener("click", function () {
            jpost(API + "/engagements/" + eid + "/checklists", { template_id: t.id }).then(function () {
              tray.hidden = true;
              load();
            });
          });
          tray.appendChild(b);
        });
      });
      // An OPEN tray with zero children paints the SAME empty dashed rectangle ext#44 reported — the
      // `[hidden]` rule only covers the closed one. `suggest` legitimately returns two empty lists
      // (fresh install, or every template `hidden`/inactive via the library page's Hide button), and
      // revealing an empty box there tells the operator nothing except that the button is broken.
      if (!tray.children.length) {
        tray.appendChild(el("p", "ckp-tray-empty",
          "No checklist templates available — add or unhide one in the checklist library."));
      }
      tray.hidden = false;
    });
  });

  load();
})();
