/* Vector editor — browser authoring UI.
 * Edits an in-memory vector.attackpath/v1 model, live-previews it through the shared VectorViewer
 * runtime, and saves/imports/exports via the JSON API. Vanilla JS, no build step.
 */
(function () {
  "use strict";
  var VV = window.VectorViewer;
  var ACCENTS = Object.keys(VV.ACCENTS);
  var EDGE_KINDS = Object.keys(VV.DEFAULT_STYLE.edgeKinds);
  var STATE_KINDS = Object.keys(VV.DEFAULT_STYLE.nodeStates);
  var TACTIC_KINDS = Object.keys(VV.DEFAULT_STYLE.tacticKinds);
  var ROLE_KINDS = Object.keys(VV.DEFAULT_STYLE.roles);
  var ROUTES = ["flow", "arcTop", "arcBot", "intra"];

  var rootEl = document.querySelector(".ved");
  if (!rootEl) return;
  var cfg = {
    id: rootEl.getAttribute("data-diagram-id"),
    apiBase: rootEl.getAttribute("data-api-base"),
    canWrite: rootEl.getAttribute("data-can-write") === "1",
    dashboard: rootEl.getAttribute("data-dashboard"),
    exportHtmlBase: rootEl.getAttribute("data-export-html-base")
  };
  var baseUrl = cfg.apiBase.replace(/\/api$/, "");
  var token = (document.querySelector('meta[name=csrf-token]') || {}).content || "";

  var model;
  try { model = JSON.parse(document.getElementById("ved-model").textContent); }
  catch (e) { model = { schema: "vector.attackpath/v1", meta: { title: "Untitled" }, zones: [], nodes: [], edges: [], phases: [{ n: 0, intro: true }] }; }
  ensureShape(model);

  var panelEl = rootEl.querySelector("[data-panel]");
  var previewEl = rootEl.querySelector("[data-preview]");
  var scrub = rootEl.querySelector("[data-scrub]");
  var phaseNum = rootEl.querySelector("[data-phase-num]");
  var previewNote = rootEl.querySelector("[data-preview-note]");
  var currentTab = "meta";
  var viewer = VV.mount(previewEl, model, { captureKeys: false });
  viewer.onPhaseChange(function (p) { scrub.value = p; phaseNum.textContent = p; });

  // ---- utilities ----------------------------------------------------------
  function ensureShape(m) {
    m.schema = "vector.attackpath/v1";
    m.meta = m.meta || {}; m.meta.intro = m.meta.intro || {};
    m.zones = m.zones || []; m.nodes = m.nodes || []; m.edges = m.edges || [];
    m.phases = m.phases || [];
    if (!m.phases.some(function (p) { return p.n === 0 || p.intro; })) m.phases.unshift({ n: 0, intro: true });
  }
  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
  function getPath(o, path) {
    var parts = path.split("."), cur = o;
    for (var i = 0; i < parts.length; i++) { if (cur == null) return undefined; var k = /^\d+$/.test(parts[i]) ? +parts[i] : parts[i]; cur = cur[k]; }
    return cur;
  }
  function setPath(o, path, val) {
    var parts = path.split("."), cur = o;
    for (var i = 0; i < parts.length - 1; i++) {
      var k = /^\d+$/.test(parts[i]) ? +parts[i] : parts[i];
      if (cur[k] == null) cur[k] = /^\d+$/.test(parts[i + 1]) ? [] : {};
      cur = cur[k];
    }
    var last = parts[parts.length - 1]; if (/^\d+$/.test(last)) last = +last;
    cur[last] = val;
  }
  var refreshTimer = null;
  function refreshPreview() {
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () {
      var keep = viewer.phase();
      viewer.setModel(model);
      var mx = viewer.max();
      scrub.max = mx;
      if (keep > mx) keep = mx;
      viewer.goto(keep);
      scrub.value = keep; phaseNum.textContent = keep;
      var pc = model.phases.filter(function (p) { return !p.intro && p.n !== 0; }).length;
      previewNote.textContent = model.nodes.length + " nodes · " + model.edges.length + " edges · " + pc + " phases";
      dirty = true;
    }, 90);
  }

  // ---- field builders -----------------------------------------------------
  function fText(label, path, opts) {
    opts = opts || {};
    var v = getPath(model, path); v = v == null ? "" : v;
    var tag = opts.textarea
      ? '<textarea data-bind="' + path + '" data-type="text" ' + (opts.rows ? 'rows="' + opts.rows + '"' : "") + '>' + esc(v) + "</textarea>"
      : '<input type="text" data-bind="' + path + '" data-type="text" value="' + esc(v) + '" ' + (opts.ph ? 'placeholder="' + esc(opts.ph) + '"' : "") + ">";
    return '<div class="ved-field"><label>' + esc(label) + "</label>" + tag + "</div>";
  }
  function fNum(label, path) {
    var v = getPath(model, path); v = (v == null ? "" : v);
    return '<div class="ved-field"><label>' + esc(label) + '</label><input type="number" data-bind="' + path + '" data-type="num" value="' + esc(v) + '"></div>';
  }
  function fSelect(label, path, options, opts) {
    opts = opts || {};
    var v = getPath(model, path); v = v == null ? "" : String(v);
    var opts_html = (opts.blank ? '<option value="">' + esc(opts.blank) + "</option>" : "");
    options.forEach(function (o) {
      var val = typeof o === "object" ? o.value : o, lbl = typeof o === "object" ? o.label : o;
      opts_html += '<option value="' + esc(val) + '"' + (String(val) === v ? " selected" : "") + ">" + esc(lbl) + "</option>";
    });
    return '<div class="ved-field"><label>' + esc(label) + '</label><select data-bind="' + path + '" data-type="text">' + opts_html + "</select></div>";
  }
  function fCheck(label, path) {
    var v = !!getPath(model, path);
    return '<div class="ved-field ved-check"><input type="checkbox" data-bind="' + path + '" data-type="bool"' + (v ? " checked" : "") + '><label>' + esc(label) + "</label></div>";
  }

  // ---- panels -------------------------------------------------------------
  function renderMeta() {
    var rail = (model.meta.railLabels || []).join(", ");
    return fText("Title", "meta.title") +
      '<div class="ved-inline">' + fText("Subtitle", "meta.subtitle") + fText("Badge", "meta.badge") + "</div>" +
      '<div class="ved-field"><label>Rail labels (comma-separated)</label><input type="text" data-bind="meta.railLabels" data-type="csv" value="' + esc(rail) + '"></div>' +
      '<div class="ved-section-h">Intro slide</div>' +
      fText("Eyebrow", "meta.intro.eyebrow") +
      fText("Objective", "meta.intro.objective", { textarea: true, rows: 3 }) +
      fText("Reading the map", "meta.intro.readingNotes", { textarea: true, rows: 3 }) +
      fText("Note", "meta.intro.note", { textarea: true, rows: 2 });
  }

  function renderZones() {
    var h = '<p class="ved-hint">Trust zones become the left→right columns (ordered). Nodes are placed into a zone + row.</p>';
    model.zones.forEach(function (z, i) {
      h += '<div class="ved-card" open><div class="ved-card-body">' +
        '<div class="ved-inline">' + fText("id", "zones." + i + ".id") + fText("Title", "zones." + i + ".title") + "</div>" +
        fText("Subtitle", "zones." + i + ".subtitle") +
        '<div class="ved-inline">' + fSelect("Accent", "zones." + i + ".accent", ACCENTS) + fNum("Order", "zones." + i + ".order") + "</div>" +
        '<div class="ved-row-tools"><button class="ved-btn sm" data-action="move-zone-up" data-i="' + i + '">▲</button>' +
        '<button class="ved-btn sm" data-action="move-zone-down" data-i="' + i + '">▼</button>' +
        '<button class="ved-btn sm danger" data-action="del-zone" data-i="' + i + '">Delete</button></div>' +
        "</div></div>";
    });
    h += '<button class="ved-btn add" data-action="add-zone">＋ Add zone</button>';
    return h;
  }

  function zoneOptions() { return model.zones.map(function (z) { return { value: z.id, label: z.title || z.id }; }); }
  function nodeOptions() { return model.nodes.map(function (n) { return { value: n.id, label: n.label || n.id }; }); }

  function renderNodes() {
    var h = '<p class="ved-hint">Hosts/assets. A node\'s <b>state timeline</b> drives how it lights up per phase.</p>';
    model.nodes.forEach(function (n, i) {
      var zTitle = (model.zones.filter(function (z) { return z.id === n.zone; })[0] || {}).title || n.zone || "?";
      h += '<details class="ved-card"><summary><span class="grow">' + esc(n.label || n.id) + '</span><span class="sub">' + esc(zTitle) + " · " + esc(n.ip || "") + "</span></summary>" +
        '<div class="ved-card-body">' +
        '<div class="ved-inline">' + fText("id", "nodes." + i + ".id") + fText("Label", "nodes." + i + ".label") + "</div>" +
        '<div class="ved-inline">' + fText("IP", "nodes." + i + ".ip") + fText("Domain", "nodes." + i + ".domain") + "</div>" +
        '<div class="ved-inline">' + fSelect("Zone", "nodes." + i + ".zone", zoneOptions()) + fNum("Row", "nodes." + i + ".row") + "</div>" +
        '<div class="ved-inline">' + fSelect("Role", "nodes." + i + ".role", ROLE_KINDS, { blank: "— none —" }) + fText("Dual-home IP", "nodes." + i + ".dualIp") + "</div>" +
        '<div class="ved-inline">' + fCheck("Context only (greyed)", "nodes." + i + ".context") + fNum("Activate at phase", "nodes." + i + ".activateAt") + "</div>" +
        renderStates(n, i) +
        renderReip(n, i) +
        '<div class="ved-row-tools"><button class="ved-btn sm danger" data-action="del-node" data-i="' + i + '">Delete node</button></div>' +
        "</div></details>";
    });
    h += '<button class="ved-btn add" data-action="add-node">＋ Add node</button>';
    return h;
  }

  function renderStates(n, i) {
    var h = '<div class="ved-section-h">State timeline</div>';
    (n.states || []).forEach(function (s, j) {
      h += '<div class="ved-mini"><div class="ved-mini-head"><span>state ' + (j + 1) + '</span><button class="ved-btn sm danger" data-action="del-node-state" data-i="' + i + '" data-j="' + j + '">✕</button></div>' +
        '<div class="ved-inline">' + fNum("At phase", "nodes." + i + ".states." + j + ".at") +
        fSelect("State", "nodes." + i + ".states." + j + ".state", STATE_KINDS, { blank: "— label only —" }) + "</div>" +
        fText("Status label", "nodes." + i + ".states." + j + ".label") + "</div>";
    });
    h += '<button class="ved-btn sm add" data-action="add-node-state" data-i="' + i + '">＋ Add state</button>';
    return h;
  }

  function renderReip(n, i) {
    if (!n.reIp) {
      return '<div style="margin-top:8px"><button class="ved-btn sm" data-action="toggle-reip" data-i="' + i + '">＋ Add re-IP event</button></div>';
    }
    return '<div class="ved-section-h">Re-IP event</div><div class="ved-mini">' +
      '<div class="ved-inline">' + fNum("At phase", "nodes." + i + ".reIp.at") + fText("New IP", "nodes." + i + ".reIp.ip") + "</div>" +
      fText("New domain", "nodes." + i + ".reIp.domain") +
      '<button class="ved-btn sm danger" data-action="toggle-reip" data-i="' + i + '">Remove re-IP</button></div>';
  }

  function renderEdges() {
    var h = '<p class="ved-hint">Attacker actions between nodes. <b>Route</b>: flow (side curve), arcTop/arcBot (over/under), intra (same column).</p>';
    var nOpts = nodeOptions();
    model.edges.forEach(function (e, i) {
      h += '<details class="ved-card"><summary><span class="grow">' + esc((e.from || "?") + " → " + (e.to || "?")) + '</span><span class="sub">' + esc(e.kind || "") + " @" + (e.at || 0) + "</span></summary>" +
        '<div class="ved-card-body">' +
        fText("id", "edges." + i + ".id") +
        '<div class="ved-inline">' + fSelect("From", "edges." + i + ".from", nOpts) + fSelect("To", "edges." + i + ".to", nOpts) + "</div>" +
        '<div class="ved-inline">' + fSelect("Kind", "edges." + i + ".kind", EDGE_KINDS) + fNum("At phase", "edges." + i + ".at") + "</div>" +
        '<div class="ved-inline">' + fSelect("Route", "edges." + i + ".route", ROUTES) + fNum("Offset / lane", "edges." + i + ".offset") + "</div>" +
        fText("Label", "edges." + i + ".label") +
        '<div class="ved-row-tools"><button class="ved-btn sm danger" data-action="del-edge" data-i="' + i + '">Delete edge</button></div>' +
        "</div></details>";
    });
    h += '<button class="ved-btn add" data-action="add-edge">＋ Add edge</button>';
    return h;
  }

  function renderPhases() {
    var h = '<p class="ved-hint">The ordered walkthrough. Phase 0 is the intro slide.</p>';
    var nOpts = nodeOptions();
    model.phases.slice().sort(function (a, b) { return (a.n || 0) - (b.n || 0); }).forEach(function (ph) {
      var i = model.phases.indexOf(ph);
      if (ph.intro || ph.n === 0) {
        h += '<details class="ved-card"><summary><span class="grow">Intro slide</span><span class="sub">phase 0</span></summary><div class="ved-card-body">' +
          '<p class="ved-hint">The intro text lives on the Meta tab.</p>' +
          '<div class="ved-row-tools"><button class="ved-btn sm danger" data-action="del-phase" data-i="' + i + '">Delete</button></div></div></details>';
        return;
      }
      h += '<details class="ved-card"><summary><span class="grow">' + esc(ph.title || "(untitled)") + '</span><span class="sub">phase ' + (ph.n || 0) + "</span></summary>" +
        '<div class="ved-card-body">' +
        '<div class="ved-inline">' + fNum("Phase #", "phases." + i + ".n") + fText("Title", "phases." + i + ".title") + "</div>" +
        fText("MITRE", "phases." + i + ".mitre") +
        renderTactics(ph, i) +
        fText("Description", "phases." + i + ".desc", { textarea: true, rows: 3 }) +
        fMulti("Targets (nodes)", "phases." + i + ".targets", nOpts, ph.targets || []) +
        fText("On the map (watch)", "phases." + i + ".watch", { textarea: true, rows: 2 }) +
        fText("Note", "phases." + i + ".note", { textarea: true, rows: 2 }) +
        renderBlue(ph, i) +
        '<div class="ved-row-tools"><button class="ved-btn sm danger" data-action="del-phase" data-i="' + i + '">Delete phase</button></div>' +
        "</div></details>";
    });
    h += '<button class="ved-btn add" data-action="add-phase">＋ Add phase</button>';
    return h;
  }

  function renderTactics(ph, i) {
    var h = '<div class="ved-section-h">Tactics</div>';
    (ph.tactics || []).forEach(function (t, j) {
      h += '<div class="ved-mini"><div class="ved-inline">' +
        fText("Label", "phases." + i + ".tactics." + j + ".label") +
        fSelect("Kind", "phases." + i + ".tactics." + j + ".kind", TACTIC_KINDS) +
        "</div><button class=\"ved-btn sm danger\" data-action=\"del-tactic\" data-i=\"" + i + "\" data-j=\"" + j + "\">✕ remove</button></div>";
    });
    h += '<button class="ved-btn sm add" data-action="add-tactic" data-i="' + i + '">＋ Add tactic</button>';
    return h;
  }

  function renderBlue(ph, i) {
    if (!ph.blue) return '<div style="margin-top:8px"><button class="ved-btn sm" data-action="toggle-blue" data-i="' + i + '">＋ Add Blue-team detection</button></div>';
    return '<div class="ved-section-h">Blue-team detection</div>' +
      fText("Tool", "phases." + i + ".blue.tool") +
      fText("Finding", "phases." + i + ".blue.finding", { textarea: true, rows: 2 }) +
      fText("Example query", "phases." + i + ".blue.query", { textarea: true, rows: 3 }) +
      fText("What is seen", "phases." + i + ".blue.seen", { textarea: true, rows: 2 }) +
      fText("Gap / caveat", "phases." + i + ".blue.note", { textarea: true, rows: 2 }) +
      fCheck("Gap / unvalidated", "phases." + i + ".blue.gap") +
      '<button class="ved-btn sm danger" data-action="toggle-blue" data-i="' + i + '">Remove Blue block</button>';
  }

  function fMulti(label, path, options, selected) {
    var sel = {}; (selected || []).forEach(function (v) { sel[v] = 1; });
    var opts = options.map(function (o) { return '<option value="' + esc(o.value) + '"' + (sel[o.value] ? " selected" : "") + ">" + esc(o.label) + "</option>"; }).join("");
    return '<div class="ved-field"><label>' + esc(label) + '</label><select multiple size="4" data-bind="' + path + '" data-type="multiselect">' + opts + "</select></div>";
  }

  function renderStyle() {
    var cur = model.style ? JSON.stringify(model.style, null, 2) : "";
    return '<p class="ved-hint">Optional style overrides (edge kinds, node states, roles, tactic colors). Leave blank to use the built-in theme. Must be valid JSON.</p>' +
      '<textarea class="ved-json" data-bind="style" data-type="json" placeholder="{ }">' + esc(cur) + "</textarea>" +
      '<div class="ved-section-h">Defaults (reference)</div>' +
      '<div class="ved-ro">' + esc(JSON.stringify(VV.DEFAULT_STYLE, null, 2)) + "</div>";
  }

  var PANELS = { meta: renderMeta, zones: renderZones, nodes: renderNodes, edges: renderEdges, phases: renderPhases, style: renderStyle };
  function renderPanel(tab) {
    currentTab = tab;
    panelEl.innerHTML = (PANELS[tab] || renderMeta)();
    rootEl.querySelectorAll(".ved-tab").forEach(function (b) { b.classList.toggle("active", b.dataset.tab === tab); });
    if (!cfg.canWrite) panelEl.querySelectorAll("input,select,textarea,button").forEach(function (el) { el.disabled = true; });
  }

  // ---- binding ------------------------------------------------------------
  panelEl.addEventListener("input", onEdit);
  panelEl.addEventListener("change", onEdit);
  function onEdit(ev) {
    var el = ev.target.closest("[data-bind]");
    if (!el) return;
    var path = el.getAttribute("data-bind"), type = el.getAttribute("data-type");
    var val;
    if (type === "num") { val = el.value === "" ? undefined : Number(el.value); if (val === undefined) { deletePath(model, path); refreshPreview(); return; } }
    else if (type === "bool") val = el.checked;
    else if (type === "csv") val = el.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    else if (type === "multiselect") val = Array.prototype.slice.call(el.selectedOptions).map(function (o) { return o.value; });
    else if (type === "json") {
      var t = el.value.trim();
      if (t === "") { delete model.style; el.classList.remove("bad"); refreshPreview(); return; }
      try { model.style = JSON.parse(t); el.classList.remove("bad"); } catch (e) { el.classList.add("bad"); return; }
      refreshPreview(); return;
    } else val = el.value;
    setPath(model, path, val);
    refreshPreview();
    // Re-IP / id label changes: refresh summaries on blur (change), not per keystroke.
    if (ev.type === "change" && (path.endsWith(".zone") || path.endsWith(".id") || path.endsWith(".label") || path.endsWith(".from") || path.endsWith(".to") || path.endsWith(".n") || path.endsWith(".title"))) {
      renderPanel(currentTab);
    }
  }
  function deletePath(o, path) {
    var parts = path.split("."), cur = o;
    for (var i = 0; i < parts.length - 1; i++) { var k = /^\d+$/.test(parts[i]) ? +parts[i] : parts[i]; if (cur == null) return; cur = cur[k]; }
    if (cur == null) return;
    var last = parts[parts.length - 1]; if (/^\d+$/.test(last)) last = +last;
    delete cur[last];
  }

  panelEl.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-action]");
    if (!btn || !cfg.canWrite) return;
    var a = btn.getAttribute("data-action"), i = +btn.getAttribute("data-i"), j = +btn.getAttribute("data-j");
    var uid = function (p) { return p + Math.random().toString(36).slice(2, 7); };
    if (a === "add-zone") model.zones.push({ id: uid("zone"), title: "New zone", subtitle: "", accent: "slate", order: model.zones.length });
    else if (a === "del-zone") model.zones.splice(i, 1);
    else if (a === "move-zone-up" || a === "move-zone-down") {
      var k = a === "move-zone-up" ? i - 1 : i + 1;
      if (k >= 0 && k < model.zones.length) { var tmp = model.zones[i]; model.zones[i] = model.zones[k]; model.zones[k] = tmp; model.zones.forEach(function (z, idx) { z.order = idx; }); }
    }
    else if (a === "add-node") model.nodes.push({ id: uid("node"), label: "new-host", ip: "", zone: (model.zones[0] || {}).id || "", row: 0, states: [] });
    else if (a === "del-node") model.nodes.splice(i, 1);
    else if (a === "add-node-state") { model.nodes[i].states = model.nodes[i].states || []; model.nodes[i].states.push({ at: 1, state: "", label: "" }); }
    else if (a === "del-node-state") model.nodes[i].states.splice(j, 1);
    else if (a === "toggle-reip") { if (model.nodes[i].reIp) delete model.nodes[i].reIp; else model.nodes[i].reIp = { at: 1, ip: "", domain: "" }; }
    else if (a === "add-edge") { var n0 = (model.nodes[0] || {}).id || "", n1 = (model.nodes[1] || model.nodes[0] || {}).id || ""; model.edges.push({ id: uid("edge"), from: n0, to: n1, kind: "attack", at: 1, route: "flow", label: "" }); }
    else if (a === "del-edge") model.edges.splice(i, 1);
    else if (a === "add-phase") { var maxN = model.phases.reduce(function (m, p) { return Math.max(m, p.n || 0); }, 0); model.phases.push({ n: maxN + 1, title: "New phase", tactics: [], mitre: "", desc: "", targets: [], watch: "" }); }
    else if (a === "del-phase") model.phases.splice(i, 1);
    else if (a === "add-tactic") { model.phases[i].tactics = model.phases[i].tactics || []; model.phases[i].tactics.push({ label: "Tactic", kind: "attack" }); }
    else if (a === "del-tactic") model.phases[i].tactics.splice(j, 1);
    else if (a === "toggle-blue") { if (model.phases[i].blue) delete model.phases[i].blue; else model.phases[i].blue = { tool: "", finding: "", query: "", seen: "", note: "", gap: false }; }
    else return;
    renderPanel(currentTab);
    refreshPreview();
  });

  // ---- tabs + scrubber ----------------------------------------------------
  rootEl.querySelectorAll(".ved-tab").forEach(function (b) {
    b.addEventListener("click", function () { renderPanel(b.dataset.tab); });
  });
  scrub.addEventListener("input", function () { var p = +scrub.value; phaseNum.textContent = p; viewer.goto(p); });

  // ---- toolbar ------------------------------------------------------------
  var dirty = false;
  function headers(json) { var h = { "X-CSRFToken": token }; if (json) h["Content-Type"] = "application/json"; return h; }
  function toast(msg, err) {
    var t = document.createElement("div"); t.className = "ved-toast" + (err ? " err" : ""); t.textContent = msg;
    document.body.appendChild(t); requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () { t.classList.remove("show"); setTimeout(function () { t.remove(); }, 250); }, 2200);
  }
  function currentName() { var el = document.getElementById("ved-name"); return (el && el.value.trim()) || "Untitled attack path"; }

  var saveBtn = document.getElementById("ved-save");
  if (saveBtn) saveBtn.addEventListener("click", function () {
    var name = currentName();
    if (cfg.id === "new") {
      fetch(cfg.apiBase + "/diagrams", { method: "POST", headers: headers(true), body: JSON.stringify({ name: name, model: model }) })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
        .then(function (jd) { location.href = baseUrl + "/edit/" + jd.id; })
        .catch(function () { toast("Save failed", true); });
    } else {
      fetch(cfg.apiBase + "/diagrams/" + cfg.id, { method: "PUT", headers: headers(true), body: JSON.stringify({ name: name, model: model }) })
        .then(function (r) { if (!r.ok) throw r; dirty = false; toast("Saved"); saveBtn.classList.add("saved"); setTimeout(function () { saveBtn.classList.remove("saved"); }, 1200); })
        .catch(function () { toast("Save failed", true); });
    }
  });

  document.getElementById("ved-export-json").addEventListener("click", function () {
    var blob = new Blob([JSON.stringify(model, null, 2)], { type: "application/json" });
    downloadBlob(blob, safeName(currentName()) + ".json");
  });
  document.getElementById("ved-export-html").addEventListener("click", function () {
    fetch(cfg.exportHtmlBase, { method: "POST", headers: headers(true), body: JSON.stringify({ model: model, title: currentName() }) })
      .then(function (r) { return r.ok ? r.blob() : Promise.reject(r); })
      .then(function (blob) { downloadBlob(blob, safeName(currentName()) + ".html"); })
      .catch(function () { toast("Export failed", true); });
  });
  function downloadBlob(blob, name) {
    var url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  function safeName(n) { return (n || "attack-path").replace(/[^a-z0-9\-_ ]/gi, "-").trim().replace(/\s+/g, "-").slice(0, 80) || "attack-path"; }

  window.addEventListener("beforeunload", function (e) { if (dirty && cfg.canWrite) { e.preventDefault(); e.returnValue = ""; } });

  // boot
  renderPanel("meta");
  refreshPreview();
})();
