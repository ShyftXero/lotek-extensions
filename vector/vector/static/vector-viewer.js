/* Vector attack-path viewer runtime.
 *
 * Renders a `vector.attackpath/v1` model into an interactive SVG walkthrough. The SAME runtime powers
 * the in-editor live preview and the exported self-contained deliverable, so the two can never diverge.
 *
 * Public API (window.VectorViewer):
 *   mount(rootEl, model, opts) -> instance
 *   DEFAULT_STYLE, ACCENTS            (so the editor can surface the vocabulary)
 * instance:
 *   setModel(model)   re-render with a new model (editor live edits)
 *   goto(p)           jump to phase index p (0 = intro)
 *   phase()           current phase index
 *   max()             highest phase index
 *   onPhaseChange(cb) subscribe to phase changes (editor scrubber sync)
 *   destroy()
 *
 * Auto-boot: if window.__VECTOR_MODEL__ is present (the deliverable), it mounts into #vap or <body>.
 */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var NW = 176, NH = 48, ROWY = 96, ROWSTEP = 60, BAND_T = 74;
  var COLSPACING = 256, LEFTPAD = 40, RIGHTPAD = 48;

  var ACCENTS = {
    red:    { line: "#ff4d5e", fill: "rgba(255,77,94,.08)",  text: "#ffdfe3" },
    orange: { line: "#ff9548", fill: "rgba(255,149,72,.08)", text: "#ffe6cf" },
    cyan:   { line: "#37c9d6", fill: "rgba(55,201,214,.07)", text: "#d6f6fa" },
    amber:  { line: "#f4b740", fill: "rgba(244,183,64,.10)", text: "#fff0cf" },
    green:  { line: "#3ecf8e", fill: "rgba(62,207,142,.08)", text: "#d7f7e7" },
    violet: { line: "#a98bff", fill: "rgba(169,139,255,.08)", text: "#ece4ff" },
    slate:  { line: "#5f7180", fill: "rgba(127,147,163,.06)", text: "#c7d3dc" }
  };

  var DEFAULT_STYLE = {
    edgeKinds: {
      attack:   { accent: "red",    width: 2,   dash: null,     flow: false },
      transfer: { accent: "red",    width: 1.6, dash: [5, 4],   flow: false, opacity: 0.9 },
      c2:       { accent: "orange", width: 1.8, dash: null,     flow: true },
      tunnel:   { accent: "cyan",   width: 1.8, dash: [7, 5],   flow: true },
      ssh:      { accent: "cyan",   width: 1.6, dash: [2, 5],   flow: false },
      disc:     { accent: "cyan",   width: 1.3, dash: [1, 4],   flow: false, opacity: 0.65 },
      mesh:     { accent: "amber",  width: 1.6, dash: [6, 4],   flow: true, both: true },
      action:   { accent: "amber",  width: 1.8, dash: null,     flow: true },
      disrupt:  { accent: "amber",  width: 2.4, dash: [5, 3],   flow: true }
    },
    nodeStates: {
      target:   { accent: "cyan",  precedence: 1, fillNode: false, label: "TARGET" },
      owned:    { accent: "red",   precedence: 3, fillNode: true,  label: "OWNED" },
      beacon:   { accent: "red",   precedence: 3, fillNode: true,  label: "BEACON", ring: "orange" },
      impacted: { accent: "amber", precedence: 4, fillNode: true,  label: "IMPACT" }
    },
    roles: {
      c2:      { accent: "orange", status: "C2" },
      rshell:  { accent: "red",    status: "REV SHELL" },
      stager:  { accent: "red",    status: "STAGER" },
      payload: { accent: "red",    status: "PAYLOAD" },
      egress:  { accent: "cyan",   status: "EGRESS", idle: true, idleStatus: "—" },
      backup:  { accent: "orange", status: "BACKUP", idle: true, idleStatus: "STANDBY" }
    },
    tacticKinds: {
      attack: "red", c2: "orange", tunnel: "cyan", disc: "cyan", evasion: "red",
      persist: "orange", mesh: "amber", impact: "amber", action: "amber", recon: "cyan"
    }
  };

  // ---- helpers ------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function isObj(x) { return x && typeof x === "object" && !Array.isArray(x); }
  function mergeStyle(over) {
    var out = JSON.parse(JSON.stringify(DEFAULT_STYLE));
    if (!isObj(over)) return out;
    ["edgeKinds", "nodeStates", "roles", "tacticKinds"].forEach(function (cat) {
      if (!isObj(over[cat])) return;
      out[cat] = out[cat] || {};
      Object.keys(over[cat]).forEach(function (k) {
        if (isObj(over[cat][k]) && isObj(out[cat][k])) {
          out[cat][k] = Object.assign({}, out[cat][k], over[cat][k]);
        } else {
          out[cat][k] = over[cat][k];
        }
      });
    });
    return out;
  }
  // Style values come from the (un-normalized) model.style — treat as untrusted. These are inserted
  // into inline style="" attributes / marker ids, so a raw string with a quote could break out and
  // inject an attribute. Restrict colors to known accents or a strict color grammar, numbers to finite
  // floats, dash to a numeric list, and marker/id tokens to a safe charset. (Text goes through esc().)
  var _COLOR_RE = /^(#[0-9a-fA-F]{3,8}|rgba?\([0-9.,%\s]+\)|[a-zA-Z]{3,20})$/;
  function safeColor(a) {
    if (ACCENTS[a]) return ACCENTS[a].line;
    if (typeof a === "string" && _COLOR_RE.test(a)) return a;
    return ACCENTS.slate.line;
  }
  function accentLine(a) { return safeColor(a); }
  function accentFill(a) { return (ACCENTS[a] && ACCENTS[a].fill) || "rgba(127,147,163,.06)"; }
  function safeNum(v, dflt) { var n = Number(v); return isFinite(n) ? n : dflt; }
  function safeDash(d) {
    if (!Array.isArray(d)) return "";
    return d.map(function (x) { return Number(x); }).filter(function (x) { return isFinite(x); }).join(" ");
  }
  function safeToken(s) { return String(s == null ? "" : s).replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 40) || "x"; }

  function computeMax(model) {
    var m = 0;
    (model.phases || []).forEach(function (p) { if (typeof p.n === "number") m = Math.max(m, p.n); });
    (model.edges || []).forEach(function (e) { m = Math.max(m, e.at || 0); });
    (model.nodes || []).forEach(function (n) {
      (n.states || []).forEach(function (s) { m = Math.max(m, s.at || 0); });
      if (n.reIp) m = Math.max(m, n.reIp.at || 0);
      if (n.activateAt != null) m = Math.max(m, n.activateAt);
    });
    return m;
  }

  function geometry(model) {
    var zones = (model.zones || []).slice().sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
    var cols = {}, i;
    for (i = 0; i < zones.length; i++) {
      cols[zones[i].id] = { x: LEFTPAD + i * COLSPACING, zone: zones[i], idx: i };
    }
    var geo = {}, maxRow = 0;
    (model.nodes || []).forEach(function (n) {
      var c = cols[n.zone];
      if (!c) return;
      var x = c.x, y = ROWY + (n.row || 0) * ROWSTEP;
      geo[n.id] = { x: x, y: y, w: NW, h: NH, cx: x + NW / 2, cy: y + NH / 2 };
      if ((n.row || 0) > maxRow) maxRow = n.row || 0;
    });
    var bandBottom = ROWY + maxRow * ROWSTEP + NH + 16;
    var width = Math.max(760, LEFTPAD + Math.max(0, zones.length - 1) * COLSPACING + NW + RIGHTPAD);
    var minLane = BAND_T - 30, maxLane = bandBottom + 20;
    (model.edges || []).forEach(function (e) {
      if (e.route === "arcTop" && typeof e.lane === "number") minLane = Math.min(minLane, e.lane);
      if (e.route === "arcBot" && typeof e.lane === "number") maxLane = Math.max(maxLane, e.lane);
    });
    var top = Math.min(BAND_T - 30, minLane - 22);
    var bottom = Math.max(bandBottom + 24, maxLane + 24);
    return {
      zones: zones, cols: cols, geo: geo, bandBottom: bandBottom, width: width,
      viewBox: "0 " + top + " " + width + " " + (bottom - top)
    };
  }

  // ---- path builders (from the reference) ---------------------------------
  function pFlow(a, b, off) {
    off = off || 0;
    var rev = b.x < a.x;
    var sx = rev ? a.x : a.x + a.w, sy = a.cy + off, ex = rev ? b.x + b.w : b.x, ey = b.cy + off;
    var dx = Math.max(38, Math.abs(ex - sx) * 0.42) * (rev ? -1 : 1);
    return { d: "M" + sx + "," + sy + " C" + (sx + dx) + "," + sy + " " + (ex - dx) + "," + ey + " " + ex + "," + ey, mx: (sx + ex) / 2, my: (sy + ey) / 2 - 7 };
  }
  function pArc(a, b, peak) {
    var sx = a.cx, sy = a.y, ex = b.cx, ey = b.y;
    return { d: "M" + sx + "," + sy + " C" + sx + "," + peak + " " + ex + "," + peak + " " + ex + "," + ey, mx: (sx + ex) / 2, my: peak + 11 };
  }
  function pArcBot(a, b, dip) {
    var sx = a.cx, sy = a.y + a.h, ex = b.cx, ey = b.y + b.h;
    return { d: "M" + sx + "," + sy + " C" + sx + "," + dip + " " + ex + "," + dip + " " + ex + "," + ey, mx: (sx + ex) / 2, my: dip - 4 };
  }
  function pIntra(a, b) {
    var gx = a.x - 18, sx = a.x, sy = a.cy, ex = b.x, ey = b.cy;
    return { d: "M" + sx + "," + sy + " C" + gx + "," + sy + " " + gx + "," + ey + " " + ex + "," + ey, mx: gx - 4, my: (sy + ey) / 2 };
  }

  // ---- node state resolution ---------------------------------------------
  function nodeVisual(node, p, style) {
    var cls = ["node"], accent = null, fill = false, statusText = "", hasBeacon = false, isNew = false, idle = false;
    var bestPrec = -1, visual = null, statusLabel = null;
    (node.states || []).forEach(function (s) {
      if (s.at > p) return;
      if (s.at === p) isNew = true;
      if (s.state && style.nodeStates[s.state]) {
        var prec = style.nodeStates[s.state].precedence || 0;
        if (prec >= bestPrec) { bestPrec = prec; visual = s.state; }
        if (style.nodeStates[s.state].ring) hasBeacon = true;
      }
      if (s.label != null && s.label !== "") statusLabel = s.label;
    });
    if (node.reIp && node.reIp.at === p) isNew = true;
    if (node.activateAt != null && node.activateAt === p) isNew = true;

    if (node.context) {
      cls.push("ctx");
      return { cls: cls, accent: null, fill: false, statusText: "", hasBeacon: false, isNew: isNew, idle: false };
    }
    var role = node.role ? style.roles[node.role] : null;
    if (role) {
      accent = role.accent; fill = true;
      idle = !!role.idle && !(node.activateAt != null && p >= node.activateAt);
      if (idle) fill = false;
    }
    if (visual) {
      var sdef = style.nodeStates[visual];
      accent = sdef.accent; fill = !!sdef.fillNode;
    }
    if (statusLabel) statusText = statusLabel;
    else if (visual) statusText = style.nodeStates[visual].label || visual.toUpperCase();
    else if (role) statusText = idle ? (role.idleStatus || "STANDBY") : role.status;

    if (idle) cls.push("idle");
    if (isNew) cls.push("is-new");
    return { cls: cls, accent: accent, fill: fill, statusText: statusText, hasBeacon: hasBeacon, isNew: isNew, idle: idle };
  }

  // ---- SVG builders -------------------------------------------------------
  function defsSvg() {
    var s = "<defs>";
    Object.keys(ACCENTS).forEach(function (a) {
      var col = ACCENTS[a].line;
      s += '<marker id="vap-ar-' + a + '" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="' + col + '"/></marker>';
    });
    return s + "</defs>";
  }

  function bandsSvg(g) {
    var s = "";
    g.zones.forEach(function (z) {
      var c = g.cols[z.id], col = accentLine(z.accent);
      s += '<rect class="band" x="' + (c.x - 16) + '" y="' + BAND_T + '" width="' + (NW + 32) + '" height="' + (g.bandBottom - BAND_T) + '" rx="8"/>';
      s += '<rect x="' + (c.x - 16) + '" y="' + BAND_T + '" width="' + (NW + 32) + '" height="3" fill="' + col + '" opacity=".85"/>';
      s += '<text class="band-title" x="' + (c.x - 14) + '" y="46">' + esc(z.title) + "</text>";
      if (z.subtitle) s += '<text class="band-cidr" x="' + (c.x - 14) + '" y="61">' + esc(z.subtitle) + "</text>";
    });
    var cy = (BAND_T + g.bandBottom) / 2;
    (function () {
      var model = g._model || {};
      (model.boundaries || []).forEach(function (f) {
        var x;
        if (typeof f.x === "number") x = f.x;
        else if (f.afterZone && g.cols[f.afterZone]) x = g.cols[f.afterZone].x + NW + (COLSPACING - NW) / 2;
        else return;
        s += '<line class="fw-line" x1="' + x + '" y1="' + (BAND_T - 2) + '" x2="' + x + '" y2="' + (g.bandBottom + 2) + '"/>';
        s += '<rect class="fw-chip" x="' + (x - 9) + '" y="' + (cy - 12) + '" width="18" height="24" rx="3"/>';
        s += '<text class="fw-ico" x="' + x + '" y="' + (cy + 4) + '" text-anchor="middle">⛬</text>';
        if (f.top) s += '<text class="fw-label" x="' + x + '" y="' + (cy + 28) + '" text-anchor="middle">' + esc(f.top) + "</text>";
        if (f.bottom) s += '<text class="fw-label" x="' + x + '" y="' + (cy + 39) + '" text-anchor="middle" fill="#4a5b69">' + esc(f.bottom) + "</text>";
      });
    })();
    return s;
  }

  function nodesSvg(model, g, p, style) {
    var s = "";
    (model.nodes || []).forEach(function (n) {
      var geo = g.geo[n.id];
      if (!geo) return;
      var v = nodeVisual(n, p, style);
      var reOn = n.reIp && p >= n.reIp.at;
      var curIp = reOn ? n.reIp.ip : n.ip;
      var curDom = reOn ? n.reIp.domain : n.domain;
      if (n.reIp && p === n.reIp.at && v.cls.indexOf("is-new") < 0) v.cls.push("is-new");
      var y1 = curDom ? 15 : 19, y2 = curDom ? 28 : 33;
      var boxStyle = "";
      if (v.accent) {
        boxStyle = 'stroke:' + accentLine(v.accent) + ';';
        if (v.fill) boxStyle += 'fill:' + accentFill(v.accent) + ';';
      }
      s += '<g class="' + v.cls.join(" ") + '" transform="translate(' + geo.x + "," + geo.y + ')">';
      s += '<rect class="box" x="0" y="0" width="' + geo.w + '" height="' + geo.h + '" rx="6"' + (boxStyle ? ' style="' + boxStyle + '"' : "") + "/>";
      s += '<text class="nm" x="10" y="' + y1 + '"' + (v.accent && v.fill ? ' style="fill:' + (ACCENTS[v.accent] ? ACCENTS[v.accent].text : "#fff") + '"' : "") + ">" + esc(n.label) + "</text>";
      s += '<text class="ip" x="10" y="' + y2 + '">' + esc(curIp) + (n.dualIp ? "  ⇄ " + esc(n.dualIp) : "") + "</text>";
      if (curDom) s += '<text class="dom" x="10" y="41">' + esc(curDom) + "</text>";
      if (v.statusText) s += '<text class="stt" x="' + (geo.w - 10) + '" y="' + y1 + '" text-anchor="end"' + (v.accent ? ' style="fill:' + accentLine(v.accent) + '"' : "") + ">" + esc(v.statusText) + "</text>";
      if (v.hasBeacon) {
        var rc = accentLine((style.nodeStates.beacon && style.nodeStates.beacon.ring) || "orange");
        s += '<circle class="beacon-ring" cx="' + (geo.w - 13) + '" cy="' + y2 + '" r="4" style="stroke:' + rc + '"/>';
        s += '<circle class="pulse-ring" cx="' + (geo.w - 13) + '" cy="' + y2 + '" r="3" fill="none" stroke="' + rc + '" stroke-width="1.2"/>';
      }
      s += "</g>";
    });
    return s;
  }

  function edgePath(e, g) {
    var a = g.geo[e.from], b = g.geo[e.to];
    if (!a || !b) return null;
    if (e.route === "arcTop") return pArc(a, b, typeof e.lane === "number" ? e.lane : BAND_T - 34);
    if (e.route === "arcBot") return pArcBot(a, b, typeof e.lane === "number" ? e.lane : g.bandBottom + 34);
    if (e.route === "intra") return pIntra(a, b);
    return pFlow(a, b, e.offset || 0);
  }

  function edgesSvg(model, g, p, style) {
    var vis = "", lab = "";
    (model.edges || []).slice().sort(function (x, y) { return (x.at || 0) - (y.at || 0); }).forEach(function (e) {
      if ((e.at || 0) > p) return;
      var pt = edgePath(e, g);
      if (!pt) return;
      var conf = style.edgeKinds[e.kind] || { accent: "slate", width: 1.6, dash: null };
      var col = accentLine(conf.accent);
      var hot = (e.at || 0) === p;
      var cls = "edge";
      var inlineDash = "";
      if (hot) { cls += " hot draw"; if (conf.flow) cls += " flow"; }
      else { cls += " dim"; if (conf.flow) cls += " flow"; inlineDash = conf.dash ? safeDash(conf.dash) : (conf.flow ? "6 5" : "0"); }
      var st = "stroke:" + col + ";stroke-width:" + safeNum(conf.width, 1.6) + ";";
      if (inlineDash) st += "stroke-dasharray:" + inlineDash + ";";
      var mk = "vap-ar-" + (conf.accent in ACCENTS ? conf.accent : "slate");
      var startMk = conf.both ? 'marker-start="url(#' + mk + ')" ' : "";
      vis += '<path class="' + cls + '" d="' + pt.d + '" style="' + st + 'color:' + col + '" ' + startMk + 'marker-end="url(#' + mk + ')"/>';
      if (hot && e.label) {
        var w = e.label.length * 5.6 + 12;
        lab += '<g transform="translate(' + (pt.mx - w / 2) + "," + (pt.my - 9) + ')"><rect class="elabel-bg" x="0" y="0" width="' + w + '" height="15" rx="3"/><text class="elabel" x="' + (w / 2) + '" y="11" text-anchor="middle" fill="#dfe9f0">' + esc(e.label) + "</text></g>";
      }
    });
    return vis + lab;
  }

  function legendSvg(style) {
    // derived from the style catalogs actually used — kept simple/static-ish
    var items = [
      { t: "sw", accent: "red", label: "Exploit / lateral" },
      { t: "sw", accent: "orange", label: "C2 beacon" },
      { t: "swd", accent: "cyan", label: "Tunnel / SSH" },
      { t: "swd", accent: "amber", label: "Mesh / disrupt" },
      { t: "bx", accent: "red", label: "owned" },
      { t: "bx", accent: "amber", label: "impacted" }
    ];
    return items.map(function (it) {
      var col = accentLine(it.accent);
      var sw = it.t === "bx"
        ? '<span class="bx" style="border-color:' + col + '"></span>'
        : (it.t === "swd" ? '<span class="swd" style="border-color:' + col + '"></span>' : '<span class="sw" style="border-color:' + col + '"></span>');
      return '<span class="lg">' + sw + esc(it.label) + "</span>";
    }).join("");
  }

  // ---- mount --------------------------------------------------------------
  function mount(root, model, opts) {
    opts = opts || {};
    root.classList.add("vap-root", "vap");
    root.innerHTML =
      '<div class="app">' +
        '<header class="top">' +
          '<div class="top-row">' +
            '<span class="brand" data-brand></span>' +
            '<span class="sub" data-sub></span>' +
            '<span class="chip-demo" data-badge></span>' +
          "</div>" +
          '<div class="rail" data-rail></div>' +
          '<div class="rail-labels" data-rail-labels></div>' +
        "</header>" +
        '<main class="grid">' +
          '<section class="stage">' +
            '<svg class="map" data-map role="img" aria-label="Attack path topology"></svg>' +
            '<div class="legend" data-legend></div>' +
          "</section>" +
          '<aside class="brief">' +
            '<div class="brief-scroll" data-brief></div>' +
            '<div class="controls">' +
              '<button class="vap-btn" data-prev>◄ Prev</button>' +
              '<button class="vap-btn primary" data-next>Next ►</button>' +
              '<span class="spacer"></span>' +
              '<button class="vap-btn play" data-play>▶ Auto</button>' +
              '<button class="vap-btn" data-reset>Reset</button>' +
            "</div>" +
          "</aside>" +
        "</main>" +
      "</div>";

    var el = {
      brand: root.querySelector("[data-brand]"), sub: root.querySelector("[data-sub]"),
      badge: root.querySelector("[data-badge]"), rail: root.querySelector("[data-rail]"),
      railLabels: root.querySelector("[data-rail-labels]"), map: root.querySelector("[data-map]"),
      legend: root.querySelector("[data-legend]"), brief: root.querySelector("[data-brief]"),
      prev: root.querySelector("[data-prev]"), next: root.querySelector("[data-next]"),
      play: root.querySelector("[data-play]"), reset: root.querySelector("[data-reset]")
    };

    var state = { p: 0, model: null, style: null, g: null, MAX: 0, tab: "red", timer: null };
    var phaseCbs = [];

    function phaseMap() {
      var m = {};
      (state.model.phases || []).forEach(function (ph) { if (typeof ph.n === "number") m[ph.n] = ph; });
      return m;
    }

    function draw() {
      var g = state.g, model = state.model, style = state.style, p = state.p;
      g._model = model;
      el.map.setAttribute("viewBox", g.viewBox);
      el.map.style.minWidth = Math.min(1288, g.width) + "px";
      el.map.innerHTML = defsSvg() + bandsSvg(g) + edgesSvg(model, g, p, style) + nodesSvg(model, g, p, style);
      el.legend.innerHTML = legendSvg(style);
    }

    function dotColor(id) {
      var n = (state.model.nodes || []).filter(function (x) { return x.id === id; })[0];
      if (!n) return ACCENTS.cyan.line;
      var v = nodeVisual(n, state.p, state.style);
      if (v.accent) return accentLine(v.accent);
      return ACCENTS.cyan.line;
    }
    function zoneTitle(id) {
      var n = (state.model.nodes || []).filter(function (x) { return x.id === id; })[0];
      if (!n) return "";
      var c = state.g.cols[n.zone];
      return c ? c.zone.title : "";
    }
    function nodeLabel(id) {
      var n = (state.model.nodes || []).filter(function (x) { return x.id === id; })[0];
      return n ? n.label : id;
    }
    function nodeIp(id) {
      var n = (state.model.nodes || []).filter(function (x) { return x.id === id; })[0];
      return n ? n.ip : "";
    }

    function renderBrief() {
      var model = state.model, p = state.p, pm = phaseMap();
      var ph = pm[p];
      var meta = model.meta || {};
      el.brand.innerHTML = "<b>◤</b> " + esc(meta.title || "Attack path");
      el.sub.textContent = meta.subtitle || "";
      el.badge.textContent = meta.badge || "";
      el.badge.style.display = meta.badge ? "" : "none";

      if (p === 0 || (ph && ph.intro)) {
        var intro = meta.intro || {};
        el.brief.innerHTML =
          '<div class="eyebrow">' + esc(intro.eyebrow || "Walkthrough") + "</div>" +
          '<div class="ph-title">' + esc(meta.title || "Attack path") + "</div>" +
          (intro.objective ? '<p class="intro-obj">' + esc(intro.objective) + "</p>" : "") +
          (intro.readingNotes ? '<div class="blk"><div class="blk-h">Reading the map</div><p class="watch">' + esc(intro.readingNotes) + "</p></div>" : "") +
          (intro.note ? '<div class="note">' + esc(intro.note) + "</div>" : "") +
          '<div class="blk"><div class="blk-h">How to drive it</div><p class="watch">→ / Next · ← / Prev · Space auto-play · Home reset · click the progress bar to jump.</p></div>';
        return;
      }
      if (!ph) {
        el.brief.innerHTML = '<div class="empty">Phase ' + p + " — no content yet.</div>";
        return;
      }
      var tacs = (ph.tactics || []).map(function (t) {
        var col = accentLine(state.style.tacticKinds[t.kind] || "slate");
        return '<span class="tac" style="color:' + col + ';border-color:' + col + '">' + esc(t.label) + "</span>";
      }).join("");
      var tgts = (ph.targets || []).map(function (id) {
        return '<div class="tgt"><span class="dot" style="background:' + dotColor(id) + '"></span><span class="thn">' + esc(nodeLabel(id)) + '</span><span class="tip">' + esc(nodeIp(id)) + '</span><span class="tz">' + esc(zoneTitle(id)) + "</span></div>";
      }).join("");
      var b = ph.blue;
      var blueHtml = "";
      if (b) {
        var toolCls = b.gap ? "blue-tool gap" : "blue-tool";
        var toolLabel = b.gap ? "Gap / unvalidated" : "Tool";
        blueHtml =
          '<div class="' + toolCls + '"><span>' + toolLabel + ":</span> " + esc(b.tool || "—") + "</div>" +
          (b.finding ? '<p class="desc">' + esc(b.finding) + "</p>" : "") +
          (b.query ? '<div class="blue-signal">Example query</div><pre class="blue-query">' + esc(b.query) + "</pre>" : "") +
          (b.seen ? '<div class="blue-seen"><b>What is seen:</b> ' + esc(b.seen) + "</div>" : "") +
          (b.note ? '<div class="blue-note ' + (b.gap ? "gap-note" : "") + '"><b>' + (b.gap ? "Gap / caveat:" : "Notes:") + "</b> " + esc(b.note) + "</div>" : "");
      } else {
        blueHtml = '<div class="empty">No blue-team detail for this phase.</div>';
      }
      el.brief.innerHTML =
        '<div class="eyebrow">Phase <b>' + String(ph.n).padStart(2, "0") + "</b> / " + state.MAX + "</div>" +
        '<div class="ph-title">' + esc(ph.title) + "</div>" +
        '<div class="tacs">' + tacs + "</div>" +
        (ph.mitre ? '<div class="mitre">' + esc(ph.mitre) + "</div>" : "") +
        '<div class="detail-tabs" role="tablist">' +
          '<button type="button" class="detail-tab ' + (state.tab === "red" ? "active" : "") + '" data-tab="red">Red Team Action</button>' +
          '<button type="button" class="detail-tab ' + (state.tab === "blue" ? "active" : "") + '" data-tab="blue">Blue Team Detection</button>' +
        "</div>" +
        '<div class="tab-pane ' + (state.tab === "red" ? "active" : "") + '" data-pane="red">' +
          (ph.desc ? '<p class="desc">' + esc(ph.desc) + "</p>" : "") +
          (tgts ? '<div class="blk"><div class="blk-h">Targets this phase</div>' + tgts + "</div>" : "") +
          (ph.watch ? '<div class="blk"><div class="blk-h">On the map</div><p class="watch">' + esc(ph.watch) + "</p></div>" : "") +
          (ph.note ? '<div class="note">' + esc(ph.note) + "</div>" : "") +
        "</div>" +
        '<div class="tab-pane ' + (state.tab === "blue" ? "active" : "") + '" data-pane="blue">' + blueHtml + "</div>";

      root.querySelectorAll(".detail-tab").forEach(function (btn) {
        btn.addEventListener("click", function () {
          state.tab = btn.dataset.tab;
          root.querySelectorAll(".detail-tab").forEach(function (b2) { b2.classList.toggle("active", b2.dataset.tab === state.tab); });
          root.querySelectorAll(".tab-pane").forEach(function (pane) { pane.classList.toggle("active", pane.dataset.pane === state.tab); });
        });
      });
    }

    function buildRail() {
      el.rail.innerHTML = "";
      for (var i = 1; i <= state.MAX; i++) {
        (function (idx) {
          var seg = document.createElement("div");
          seg.className = "seg";
          seg.title = "Phase " + idx;
          seg.addEventListener("click", function () { stopAuto(); go(idx); });
          el.rail.appendChild(seg);
        })(i);
      }
      var labels = (state.model.meta && state.model.meta.railLabels) || [];
      el.railLabels.innerHTML = labels.map(function (l) { return "<span>" + esc(l) + "</span>"; }).join("");
    }
    function paintRail() {
      var kids = el.rail.children;
      for (var i = 0; i < kids.length; i++) {
        var idx = i + 1;
        kids[i].classList.toggle("done", idx < state.p);
        kids[i].classList.toggle("cur", idx === state.p);
      }
    }

    function render() {
      draw();
      renderBrief();
      paintRail();
      el.prev.disabled = state.p <= 0;
      el.next.disabled = state.p >= state.MAX;
      el.next.textContent = state.p >= state.MAX ? "Complete" : "Next ►";
    }

    function go(p) {
      state.p = Math.max(0, Math.min(state.MAX, p));
      render();
      phaseCbs.forEach(function (cb) { try { cb(state.p); } catch (e) {} });
    }
    function step(d) { stopAuto(); go(state.p + d); }
    function stopAuto() { if (state.timer) { clearInterval(state.timer); state.timer = null; el.play.classList.remove("on"); el.play.textContent = "▶ Auto"; } }
    function startAuto() {
      if (state.p >= state.MAX) go(0);
      el.play.classList.add("on"); el.play.textContent = "❚❚ Pause";
      state.timer = setInterval(function () { if (state.p >= state.MAX) { stopAuto(); return; } go(state.p + 1); }, 2600);
    }

    el.prev.addEventListener("click", function () { step(-1); });
    el.next.addEventListener("click", function () { step(1); });
    el.reset.addEventListener("click", function () { stopAuto(); go(0); });
    el.play.addEventListener("click", function () { state.timer ? stopAuto() : startAuto(); });

    function onKey(e) {
      if (opts.captureKeys === false) return;
      var t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "Home") { e.preventDefault(); stopAuto(); go(0); }
      else if (e.code === "Space" && opts.captureKeys) { e.preventDefault(); state.timer ? stopAuto() : startAuto(); }
    }
    if (opts.captureKeys) document.addEventListener("keydown", onKey);

    function setModel(m) {
      state.model = m || {};
      state.style = mergeStyle(state.model.style);
      state.g = geometry(state.model);
      state.MAX = computeMax(state.model);
      if (state.p > state.MAX) state.p = state.MAX;
      render();
    }

    setModel(model);
    if (opts.phase != null) go(opts.phase);

    return {
      setModel: setModel,
      goto: function (p) { stopAuto(); go(p); },
      phase: function () { return state.p; },
      max: function () { return state.MAX; },
      onPhaseChange: function (cb) { if (typeof cb === "function") phaseCbs.push(cb); },
      destroy: function () { stopAuto(); if (opts.captureKeys) document.removeEventListener("keydown", onKey); root.innerHTML = ""; }
    };
  }

  var VectorViewer = { mount: mount, DEFAULT_STYLE: DEFAULT_STYLE, ACCENTS: ACCENTS };
  if (typeof window !== "undefined") window.VectorViewer = VectorViewer;

  // Auto-boot the deliverable.
  function boot() {
    if (typeof window === "undefined" || !window.__VECTOR_MODEL__) return;
    var host = document.getElementById("vap") || document.body;
    mount(host, window.__VECTOR_MODEL__, { captureKeys: true });
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})();
