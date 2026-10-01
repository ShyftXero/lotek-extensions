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

  // ---- DOM builders -------------------------------------------------------
  // The panels are built from nodes, never from an HTML string: model text goes in through textContent /
  // createTextNode and attributes through setAttribute, so a string in the model (which may be an
  // imported or hand-edited file) is never parsed as markup. attrs: attribute name -> value (null / false
  // = omit; true = present with no value), plus `text` (textContent) and `css` (prop -> value, through
  // style.setProperty). kids: nodes, arrays of nodes, null / false (skipped); loose text goes in as t(s).
  function el(tag, attrs, kids) {
    var node = document.createElement(tag);
    for (var k in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
      var v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "text") node.textContent = str(v);
      else if (k === "css") { for (var c in v) if (Object.prototype.hasOwnProperty.call(v, c)) node.style.setProperty(c, v[c]); }
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    append(node, kids);
    return node;
  }
  function append(node, kids) {
    (kids || []).forEach(function (c) {
      if (Array.isArray(c)) append(node, c);
      else if (c != null && c !== false) node.appendChild(c);
    });
    return node;
  }
  function str(s) { return String(s == null ? "" : s); }
  function t(s) { return document.createTextNode(str(s)); }
  function btn(cls, action, text, i, j) {
    return el("button", { "class": cls, "data-action": action, "data-i": i, "data-j": j, text: text });
  }
  function inline(kids) { return el("div", { "class": "ved-inline" }, kids); }
  function sectionH(text) { return el("div", { "class": "ved-section-h", text: text }); }
  function hint(kids) { return el("p", { "class": "ved-hint" }, kids); }
  function rowTools(kids) { return el("div", { "class": "ved-row-tools" }, kids); }
  function field(label, control) { return el("div", { "class": "ved-field" }, [el("label", { text: label }), control]); }

  // ---- field builders -----------------------------------------------------
  function fText(label, path, opts) {
    opts = opts || {};
    var v = getPath(model, path); v = v == null ? "" : v;
    var control = opts.textarea
      ? el("textarea", { "data-bind": path, "data-type": "text", rows: opts.rows || null, text: v })
      : el("input", { type: "text", "data-bind": path, "data-type": "text", value: str(v), placeholder: opts.ph || null });
    return field(label, control);
  }
  function fNum(label, path, opts) {
    opts = opts || {};
    var v = getPath(model, path); v = (v == null ? "" : v);
    var attrs = { type: "number", "data-bind": path, "data-type": "num", value: str(v) };
    if (opts.min != null) attrs.min = str(opts.min);
    if (opts.max != null) attrs.max = str(opts.max);
    return field(label, el("input", attrs));
  }
  function option(value, label, selected) {
    return el("option", { value: str(value), selected: !!selected, text: label });
  }
  function fSelect(label, path, options, opts) {
    opts = opts || {};
    var v = getPath(model, path); v = v == null ? "" : String(v);
    var kids = opts.blank ? [option("", opts.blank)] : [];
    options.forEach(function (o) {
      var val = typeof o === "object" ? o.value : o, lbl = typeof o === "object" ? o.label : o;
      kids.push(option(val, lbl, String(val) === v));
    });
    return field(label, el("select", { "data-bind": path, "data-type": "text" }, kids));
  }
  function fCheck(label, path) {
    var v = !!getPath(model, path);
    return el("div", { "class": "ved-field ved-check" }, [
      el("input", { type: "checkbox", "data-bind": path, "data-type": "bool", checked: v }),
      el("label", { text: label })
    ]);
  }

  // ---- panels -------------------------------------------------------------
  function renderMeta() {
    var rail = (model.meta.railLabels || []).join(", ");
    return [
      fText("Title", "meta.title"),
      inline([fText("Subtitle", "meta.subtitle"), fText("Badge", "meta.badge")]),
      field("Rail labels (comma-separated)", el("input", { type: "text", "data-bind": "meta.railLabels", "data-type": "csv", value: rail })),
      sectionH("Intro slide"),
      fText("Eyebrow", "meta.intro.eyebrow"),
      fText("Objective", "meta.intro.objective", { textarea: true, rows: 3 }),
      fText("Reading the map", "meta.intro.readingNotes", { textarea: true, rows: 3 }),
      fText("Note", "meta.intro.note", { textarea: true, rows: 2 })
    ];
  }

  function renderZones() {
    var out = [hint([t("Trust zones become the left→right columns (ordered). Nodes are placed into a zone + row.")])];
    model.zones.forEach(function (z, i) {
      out.push(el("div", { "class": "ved-card", open: true }, [el("div", { "class": "ved-card-body" }, [
        inline([fText("id", "zones." + i + ".id"), fText("Title", "zones." + i + ".title")]),
        fText("Subtitle", "zones." + i + ".subtitle"),
        inline([fSelect("Accent", "zones." + i + ".accent", ACCENTS), fNum("Order", "zones." + i + ".order")]),
        rowTools([btn("ved-btn sm", "move-zone-up", "▲", i), btn("ved-btn sm", "move-zone-down", "▼", i),
          btn("ved-btn sm danger", "del-zone", "Delete", i)])
      ])]));
    });
    out.push(btn("ved-btn add", "add-zone", "＋ Add zone"));
    return out;
  }

  function zoneOptions() { return model.zones.map(function (z) { return { value: z.id, label: z.title || z.id }; }); }
  function nodeOptions() { return model.nodes.map(function (n) { return { value: n.id, label: n.label || n.id }; }); }

  function card(title, sub, body) {
    return el("details", { "class": "ved-card" }, [
      el("summary", {}, [el("span", { "class": "grow", text: title }), el("span", { "class": "sub", text: sub })]),
      el("div", { "class": "ved-card-body" }, body)
    ]);
  }

  function renderNodes() {
    var out = [hint([t("Hosts/assets. A node's "), el("b", { text: "state timeline" }), t(" drives how it lights up per phase.")])];
    model.nodes.forEach(function (n, i) {
      var zTitle = (model.zones.filter(function (z) { return z.id === n.zone; })[0] || {}).title || n.zone || "?";
      out.push(card(str(n.label || n.id), str(zTitle) + " · " + str(n.ip || ""), [
        inline([fText("id", "nodes." + i + ".id"), fText("Label", "nodes." + i + ".label")]),
        inline([fText("IP", "nodes." + i + ".ip"), fText("Domain", "nodes." + i + ".domain")]),
        inline([fSelect("Zone", "nodes." + i + ".zone", zoneOptions()), fNum("Row", "nodes." + i + ".row")]),
        inline([fSelect("Role", "nodes." + i + ".role", ROLE_KINDS, { blank: "— none —" }), fText("Dual-home IP", "nodes." + i + ".dualIp")]),
        inline([fCheck("Context only (greyed)", "nodes." + i + ".context"), fNum("Activate at phase", "nodes." + i + ".activateAt")]),
        renderStates(n, i),
        renderReip(n, i),
        rowTools([btn("ved-btn sm danger", "del-node", "Delete node", i)])
      ]));
    });
    out.push(btn("ved-btn add", "add-node", "＋ Add node"));
    return out;
  }

  function renderStates(n, i) {
    var out = [sectionH("State timeline")];
    (n.states || []).forEach(function (s, j) {
      out.push(el("div", { "class": "ved-mini" }, [
        el("div", { "class": "ved-mini-head" }, [el("span", { text: "state " + (j + 1) }), btn("ved-btn sm danger", "del-node-state", "✕", i, j)]),
        inline([fNum("At phase", "nodes." + i + ".states." + j + ".at"),
          fSelect("State", "nodes." + i + ".states." + j + ".state", STATE_KINDS, { blank: "— label only —" })]),
        fText("Status label", "nodes." + i + ".states." + j + ".label")
      ]));
    });
    out.push(btn("ved-btn sm add", "add-node-state", "＋ Add state", i));
    return out;
  }

  function renderReip(n, i) {
    if (!n.reIp) {
      return el("div", { css: { "margin-top": "8px" } }, [btn("ved-btn sm", "toggle-reip", "＋ Add re-IP event", i)]);
    }
    return [sectionH("Re-IP event"), el("div", { "class": "ved-mini" }, [
      inline([fNum("At phase", "nodes." + i + ".reIp.at"), fText("New IP", "nodes." + i + ".reIp.ip")]),
      fText("New domain", "nodes." + i + ".reIp.domain"),
      btn("ved-btn sm danger", "toggle-reip", "Remove re-IP", i)
    ])];
  }

  function renderEdges() {
    var out = [hint([t("Attacker actions between nodes. "), el("b", { text: "Route" }),
      t(": flow (side curve), arcTop/arcBot (over/under), intra (same column).")])];
    var nOpts = nodeOptions();
    model.edges.forEach(function (e, i) {
      out.push(card(str(e.from || "?") + " → " + str(e.to || "?"), str(e.kind || "") + " @" + str(e.at || 0), [
        fText("id", "edges." + i + ".id"),
        inline([fSelect("From", "edges." + i + ".from", nOpts), fSelect("To", "edges." + i + ".to", nOpts)]),
        inline([fSelect("Kind", "edges." + i + ".kind", EDGE_KINDS), fNum("At phase", "edges." + i + ".at")]),
        inline([fSelect("Route", "edges." + i + ".route", ROUTES), fNum("Offset / lane", "edges." + i + ".offset")]),
        fText("Label", "edges." + i + ".label"),
        rowTools([btn("ved-btn sm danger", "del-edge", "Delete edge", i)])
      ]));
    });
    out.push(btn("ved-btn add", "add-edge", "＋ Add edge"));
    return out;
  }

  function renderPhases() {
    var out = [hint([t("The ordered walkthrough. Phase 0 is the intro slide.")])];
    var nOpts = nodeOptions();
    model.phases.slice().sort(function (a, b) { return (a.n || 0) - (b.n || 0); }).forEach(function (ph) {
      var i = model.phases.indexOf(ph);
      if (ph.intro) {
        out.push(card("Intro slide", "phase 0", [
          hint([t("The intro text lives on the Meta tab.")]),
          rowTools([btn("ved-btn sm danger", "del-phase", "Delete", i)])
        ]));
        return;
      }
      out.push(card(str(ph.title || "(untitled)"), "phase " + str(ph.n || 0), [
        inline([fNum("Phase #", "phases." + i + ".n", { min: 1 }), fText("Title", "phases." + i + ".title")]),
        fText("MITRE", "phases." + i + ".mitre"),
        renderTactics(ph, i),
        fText("Description", "phases." + i + ".desc", { textarea: true, rows: 3 }),
        fMulti("Targets (nodes)", "phases." + i + ".targets", nOpts, ph.targets || []),
        fText("On the map (watch)", "phases." + i + ".watch", { textarea: true, rows: 2 }),
        fText("Note", "phases." + i + ".note", { textarea: true, rows: 2 }),
        renderBlue(ph, i),
        rowTools([btn("ved-btn sm danger", "del-phase", "Delete phase", i)])
      ]));
    });
    out.push(btn("ved-btn add", "add-phase", "＋ Add phase"));
    return out;
  }

  function renderTactics(ph, i) {
    var out = [sectionH("Tactics")];
    (ph.tactics || []).forEach(function (tc, j) {
      out.push(el("div", { "class": "ved-mini" }, [
        inline([fText("Label", "phases." + i + ".tactics." + j + ".label"),
          fSelect("Kind", "phases." + i + ".tactics." + j + ".kind", TACTIC_KINDS)]),
        btn("ved-btn sm danger", "del-tactic", "✕ remove", i, j)
      ]));
    });
    out.push(btn("ved-btn sm add", "add-tactic", "＋ Add tactic", i));
    return out;
  }

  function renderBlue(ph, i) {
    if (!ph.blue) return el("div", { css: { "margin-top": "8px" } }, [btn("ved-btn sm", "toggle-blue", "＋ Add Blue-team detection", i)]);
    return [
      sectionH("Blue-team detection"),
      fText("Tool", "phases." + i + ".blue.tool"),
      fText("Finding", "phases." + i + ".blue.finding", { textarea: true, rows: 2 }),
      fText("Example query", "phases." + i + ".blue.query", { textarea: true, rows: 3 }),
      fText("What is seen", "phases." + i + ".blue.seen", { textarea: true, rows: 2 }),
      fText("Gap / caveat", "phases." + i + ".blue.note", { textarea: true, rows: 2 }),
      fCheck("Gap / unvalidated", "phases." + i + ".blue.gap"),
      btn("ved-btn sm danger", "toggle-blue", "Remove Blue block", i)
    ];
  }

  function fMulti(label, path, options, selected) {
    var sel = {}; (selected || []).forEach(function (v) { sel[v] = 1; });
    var kids = options.map(function (o) { return option(o.value, o.label, sel[o.value]); });
    return field(label, el("select", { multiple: true, size: 4, "data-bind": path, "data-type": "multiselect" }, kids));
  }

  function renderStyle() {
    var cur = model.style ? JSON.stringify(model.style, null, 2) : "";
    return [
      hint([t("Optional style overrides (edge kinds, node states, roles, tactic colors). Leave blank to use the built-in theme. Must be valid JSON.")]),
      el("textarea", { "class": "ved-json", "data-bind": "style", "data-type": "json", placeholder: "{ }", text: cur }),
      sectionH("Defaults (reference)"),
      el("div", { "class": "ved-ro", text: JSON.stringify(VV.DEFAULT_STYLE, null, 2) })
    ];
  }

  var PANELS = { meta: renderMeta, zones: renderZones, nodes: renderNodes, edges: renderEdges, phases: renderPhases, style: renderStyle };
  function renderPanel(tab) {
    currentTab = tab;
    while (panelEl.firstChild) panelEl.removeChild(panelEl.firstChild);
    append(panelEl, [(PANELS[tab] || renderMeta)()]);
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

  // ---- resizable split ----------------------------------------------------
  // A draggable divider sets the editor-column width (--ved-split) so the operator can widen the
  // preview to see a whole attack path. The choice persists in localStorage; double-click resets.
  (function () {
    var divider = rootEl.querySelector("[data-divider]");
    if (!divider) return;
    var KEY = "vector.ved-split", dragging = false, saved = null;
    try { saved = localStorage.getItem(KEY); } catch (e) { /* private mode */ }
    if (saved) rootEl.style.setProperty("--ved-split", saved);
    function onMove(ev) {
      var r = rootEl.getBoundingClientRect();
      var pct = ((ev.clientX - r.left) / r.width) * 100;
      pct = Math.max(20, Math.min(70, pct));
      rootEl.style.setProperty("--ved-split", pct.toFixed(1) + "%");
    }
    function stop() {
      if (!dragging) return;
      dragging = false;
      divider.classList.remove("dragging");
      document.body.style.cursor = ""; document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", stop);
      try { localStorage.setItem(KEY, rootEl.style.getPropertyValue("--ved-split") || ""); } catch (e) { /* ignore */ }
    }
    divider.addEventListener("mousedown", function (ev) {
      ev.preventDefault(); dragging = true;
      divider.classList.add("dragging");
      document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", stop);
    });
    divider.addEventListener("dblclick", function () {
      rootEl.style.removeProperty("--ved-split");
      try { localStorage.removeItem(KEY); } catch (e) { /* ignore */ }
    });
  })();

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
        // Relative to this page (<base>/new), so the target is <base>/edit/<id>; the id is encoded.
        .then(function (jd) { location.assign("edit/" + encodeURIComponent(String(jd.id))); })
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
