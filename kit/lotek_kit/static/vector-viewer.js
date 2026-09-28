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
 * Auto-boot: a page carrying the model as <script type="application/json" id="vap-model"> (the
 * deliverable) mounts into #vap or <body> — no inline script needed, so it runs under
 * `script-src 'self'`. window.__VECTOR_MODEL__, when set, still wins.
 *
 * Modes: `meta.mode = "tour"` is a neutral guided tour (no red/blue tabs, "Step NN" headings, the
 * current step's targets highlighted on the map for that step only). Anything else is the attack path.
 *
 * CSP: nothing here writes a style="" attribute or evals. Per-kind colours go onto element.style (the
 * CSSOM), which a strict `style-src 'self'` does not govern.
 *
 * No HTML parsing: every element is built with createElement / createElementNS, text goes in through
 * textContent and attributes through setAttribute, so no model string ever reaches innerHTML (this file
 * is served publicly, unauthenticated, from /_kit/). vector/tests/test_viewer_static.py fails if an HTML
 * sink comes back.
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

  var RAIL_MAX = 200;  // the most segments the progress rail draws; schema.py caps phases at 200 too

  // ---- helpers ------------------------------------------------------------
  function str(s) { return String(s == null ? "" : s); }
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
  // Style values come from the (un-normalized) model.style — treat as untrusted. They reach
  // style.setProperty() and presentation attributes, never markup, but are still narrowed: colors to known
  // accents or a strict color grammar, numbers to finite floats, dash to a numeric list, and marker/id
  // tokens to a safe charset.
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

  // Step image/link targets — the same rule as schema.py's _tour_* helpers, re-applied here because the
  // editor previews an un-normalized model. Same-origin only: no scheme, no //host, no backslash, no
  // whitespace/control chars (a browser strips those before reading the scheme: "java\tscript:").
  var _OFFSITE = /^\/\/|\\|[\s\x00-\x1f\x7f]|^[^\/?#]*:/;
  var _DATA_IMAGE = /^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+\/]*={0,2}$/;
  var _DOCS_PATH = /^\/docs(?:[\/#?]|$)/;
  function sameOrigin(s) { return typeof s === "string" && s !== "" && !_OFFSITE.test(s); }
  // The checks above are what make a URL safe. encodeURI on the way out only makes the attribute value
  // unmistakably inert (no quote, angle bracket or non-ASCII survives it); existing %XX escapes are put
  // back so a pre-encoded URL is not double-encoded. A lone surrogate makes encodeURI throw: refuse it.
  function inertUrl(s) {
    try { return encodeURI(s).replace(/%25([0-9A-Fa-f]{2})/g, "%$1"); } catch (e) { return ""; }
  }
  function safeImage(s) {
    s = typeof s === "string" ? s.trim() : "";
    return (_DATA_IMAGE.test(s) || sameOrigin(s)) ? inertUrl(s) : "";
  }
  function safeHref(s) {
    s = typeof s === "string" ? s.trim() : "";
    if (!sameOrigin(s)) return "";
    if (s.charAt(0) === "/" && !_DOCS_PATH.test(s)) return "";
    return inertUrl(s);
  }

  // ---- DOM builders -------------------------------------------------------
  // attrs: attribute name -> value (null / false = omit), plus two keys that are not attributes: `text`
  // (textContent) and `css` (prop -> value, through style.setProperty, which refuses anything that is not
  // a valid value for that property). kids: nodes only (null / false skipped) — loose text goes in as
  // txt(s), so a string can never reach appendChild by accident.
  function build(node, attrs, kids) {
    for (var k in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
      var v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "text") node.textContent = String(v);
      else if (k === "css") setCss(node, v);
      else node.setAttribute(k, String(v));
    }
    return append(node, kids);
  }
  function append(node, kids) {
    (kids || []).forEach(function (c) {
      if (c != null && c !== false) node.appendChild(c);
    });
    return node;
  }
  function setCss(node, decls) {
    Object.keys(decls).forEach(function (prop) {
      if (decls[prop] != null && decls[prop] !== "") node.style.setProperty(prop, String(decls[prop]));
    });
  }
  function txt(s) { return document.createTextNode(str(s)); }
  function h(tag, attrs, kids) { return build(document.createElement(tag), attrs, kids); }
  function svg(tag, attrs, kids) { return build(document.createElementNS(NS, tag), attrs, kids); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function fill(node, kids) { clear(node); return append(node, kids); }
  function isTourModel(model) { return !!(model && model.meta && model.meta.mode === "tour"); }

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
    return svg("defs", null, Object.keys(ACCENTS).map(function (a) {
      return svg("marker", { id: "vap-ar-" + a, viewBox: "0 0 10 10", refX: 8.5, refY: 5, markerWidth: 7,
                             markerHeight: 7, orient: "auto-start-reverse" },
        [svg("path", { d: "M0,0 L10,5 L0,10 z", fill: ACCENTS[a].line })]);
    }));
  }

  function bandsSvg(g) {
    var out = [];
    g.zones.forEach(function (z) {
      var c = g.cols[z.id], col = accentLine(z.accent);
      out.push(svg("rect", { "class": "band", x: c.x - 16, y: BAND_T, width: NW + 32, height: g.bandBottom - BAND_T, rx: 8 }));
      out.push(svg("rect", { x: c.x - 16, y: BAND_T, width: NW + 32, height: 3, fill: col, opacity: ".85" }));
      out.push(svg("text", { "class": "band-title", x: c.x - 14, y: 46, text: str(z.title) }));
      if (z.subtitle) out.push(svg("text", { "class": "band-cidr", x: c.x - 14, y: 61, text: z.subtitle }));
    });
    var cy = (BAND_T + g.bandBottom) / 2;
    ((g._model || {}).boundaries || []).forEach(function (f) {
      var x;
      if (typeof f.x === "number") x = f.x;
      else if (f.afterZone && g.cols[f.afterZone]) x = g.cols[f.afterZone].x + NW + (COLSPACING - NW) / 2;
      else return;
      out.push(svg("line", { "class": "fw-line", x1: x, y1: BAND_T - 2, x2: x, y2: g.bandBottom + 2 }));
      out.push(svg("rect", { "class": "fw-chip", x: x - 9, y: cy - 12, width: 18, height: 24, rx: 3 }));
      out.push(svg("text", { "class": "fw-ico", x: x, y: cy + 4, "text-anchor": "middle", text: "⛬" }));
      if (f.top) out.push(svg("text", { "class": "fw-label", x: x, y: cy + 28, "text-anchor": "middle", text: f.top }));
      if (f.bottom) out.push(svg("text", { "class": "fw-label", x: x, y: cy + 39, "text-anchor": "middle", fill: "#4a5b69", text: f.bottom }));
    });
    return out;
  }

  function nodesSvg(model, g, p, style, focus) {
    var out = [];
    (model.nodes || []).forEach(function (n) {
      var geo = g.geo[n.id];
      if (!geo) return;
      var v = nodeVisual(n, p, style);
      var reOn = n.reIp && p >= n.reIp.at;
      var curIp = reOn ? n.reIp.ip : n.ip;
      var curDom = reOn ? n.reIp.domain : n.domain;
      if (n.reIp && p === n.reIp.at && v.cls.indexOf("is-new") < 0) v.cls.push("is-new");
      var y1 = curDom ? 15 : 19, y2 = curDom ? 28 : 33;
      var box = {};
      if (v.accent) {
        box.stroke = accentLine(v.accent);
        if (v.fill) box.fill = accentFill(v.accent);
      }
      var focused = !!(focus && focus[n.id]);
      if (focused) v.cls.push("is-focus");
      var kids = [];
      if (focused) kids.push(svg("rect", { "class": "focus-ring", x: -5, y: -5, width: geo.w + 10, height: geo.h + 10, rx: 9 }));
      kids.push(svg("rect", { "class": "box", x: 0, y: 0, width: geo.w, height: geo.h, rx: 6, css: box }));
      kids.push(svg("text", { "class": "nm", x: 10, y: y1, text: str(n.label),
                              css: v.accent && v.fill ? { fill: ACCENTS[v.accent] ? ACCENTS[v.accent].text : "#fff" } : null }));
      kids.push(svg("text", { "class": "ip", x: 10, y: y2, text: str(curIp) + (n.dualIp ? "  ⇄ " + str(n.dualIp) : "") }));
      if (curDom) kids.push(svg("text", { "class": "dom", x: 10, y: 41, text: curDom }));
      if (v.statusText) kids.push(svg("text", { "class": "stt", x: geo.w - 10, y: y1, "text-anchor": "end", text: v.statusText,
                                                css: v.accent ? { fill: accentLine(v.accent) } : null }));
      if (v.hasBeacon) {
        var rc = accentLine((style.nodeStates.beacon && style.nodeStates.beacon.ring) || "orange");
        kids.push(svg("circle", { "class": "beacon-ring", cx: geo.w - 13, cy: y2, r: 4, css: { stroke: rc } }));
        kids.push(svg("circle", { "class": "pulse-ring", cx: geo.w - 13, cy: y2, r: 3, fill: "none", stroke: rc, "stroke-width": 1.2 }));
      }
      out.push(svg("g", { "class": v.cls.join(" "), transform: "translate(" + geo.x + "," + geo.y + ")" }, kids));
    });
    return out;
  }

  function edgePath(e, g) {
    var a = g.geo[e.from], b = g.geo[e.to];
    if (!a || !b) return null;
    if (e.route === "arcTop") return pArc(a, b, typeof e.lane === "number" ? e.lane : BAND_T - 34);
    if (e.route === "arcBot") return pArcBot(a, b, typeof e.lane === "number" ? e.lane : g.bandBottom + 34);
    if (e.route === "intra") return pIntra(a, b);
    return pFlow(a, b, safeNum(e.offset, 0));  // a raw string offset would otherwise land in d=""
  }

  function edgesSvg(model, g, p, style) {
    var vis = [], lab = [];
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
      var css = { stroke: col, "stroke-width": safeNum(conf.width, 1.6), "stroke-dasharray": inlineDash, color: col };
      var mk = "url(#vap-ar-" + (conf.accent in ACCENTS ? conf.accent : "slate") + ")";
      vis.push(svg("path", { "class": cls, d: pt.d, css: css, "marker-start": conf.both ? mk : null, "marker-end": mk }));
      if (hot && e.label) {
        var w = str(e.label).length * 5.6 + 12;
        lab.push(svg("g", { transform: "translate(" + (pt.mx - w / 2) + "," + (pt.my - 9) + ")" }, [
          svg("rect", { "class": "elabel-bg", x: 0, y: 0, width: w, height: 15, rx: 3 }),
          svg("text", { "class": "elabel", x: w / 2, y: 11, "text-anchor": "middle", fill: "#dfe9f0", text: e.label })
        ]));
      }
    });
    return vis.concat(lab);
  }

  function legendItem(swatchClass, col, label) {
    return h("span", { "class": "lg" }, [h("span", { "class": swatchClass, css: col ? { "border-color": col } : null }), txt(label)]);
  }

  function legendSvg() {
    // derived from the style catalogs actually used — kept simple/static-ish
    return [
      { t: "sw", accent: "red", label: "Exploit / lateral" },
      { t: "sw", accent: "orange", label: "C2 beacon" },
      { t: "swd", accent: "cyan", label: "Tunnel / SSH" },
      { t: "swd", accent: "amber", label: "Mesh / disrupt" },
      { t: "bx", accent: "red", label: "owned" },
      { t: "bx", accent: "amber", label: "impacted" }
    ].map(function (it) { return legendItem(it.t, accentLine(it.accent), it.label); });
  }

  // Tour legend: only the edge kinds and node states this model actually uses, named by the style's own
  // `label` (falling back to the key), plus the per-step highlight. No attack vocabulary.
  function tourLegendSvg(model, style) {
    var out = [], seen = {};
    (model.edges || []).forEach(function (e) {
      if (seen["e:" + e.kind]) return;
      seen["e:" + e.kind] = 1;
      var conf = style.edgeKinds[e.kind] || { accent: "slate" };
      out.push(legendItem(conf.dash ? "swd" : "sw", accentLine(conf.accent), str(conf.label || e.kind)));
    });
    (model.nodes || []).forEach(function (n) {
      (n.states || []).forEach(function (st) {
        if (!st.state || seen["s:" + st.state] || !style.nodeStates[st.state]) return;
        seen["s:" + st.state] = 1;
        var def = style.nodeStates[st.state];
        out.push(legendItem("bx", accentLine(def.accent), String(def.label || st.state).toLowerCase()));
      });
    });
    out.push(legendItem("bx focus", null, "this step"));
    return out;
  }

  // ---- mount --------------------------------------------------------------
  function mount(root, model, opts) {
    opts = opts || {};
    root.classList.add("vap-root", "vap");
    var el = {};
    clear(root);
    root.appendChild(h("div", { "class": "app" }, [
      h("header", { "class": "top" }, [
        h("div", { "class": "top-row" }, [
          el.brand = h("span", { "class": "brand", "data-brand": "" }),
          el.sub = h("span", { "class": "sub", "data-sub": "" }),
          el.badge = h("span", { "class": "chip-demo", "data-badge": "" })
        ]),
        el.rail = h("div", { "class": "rail", "data-rail": "" }),
        el.railLabels = h("div", { "class": "rail-labels", "data-rail-labels": "" })
      ]),
      h("main", { "class": "grid" }, [
        h("section", { "class": "stage" }, [
          el.map = svg("svg", { "class": "map", "data-map": "", role: "img", "aria-label": "Diagram" }),
          el.legend = h("div", { "class": "legend", "data-legend": "" })
        ]),
        h("aside", { "class": "brief" }, [
          el.brief = h("div", { "class": "brief-scroll", "data-brief": "" }),
          h("div", { "class": "controls" }, [
            el.prev = h("button", { "class": "vap-btn", "data-prev": "", text: "◄ Prev" }),
            el.next = h("button", { "class": "vap-btn primary", "data-next": "", text: "Next ►" }),
            h("span", { "class": "spacer" }),
            el.play = h("button", { "class": "vap-btn play", "data-play": "", text: "▶ Auto" }),
            el.reset = h("button", { "class": "vap-btn", "data-reset": "", text: "Reset" })
          ])
        ])
      ])
    ]));

    var state = { p: 0, model: null, style: null, g: null, MAX: 0, tab: "red", timer: null };
    var phaseCbs = [];

    function phaseMap() {
      var m = {};
      (state.model.phases || []).forEach(function (ph) { if (typeof ph.n === "number") m[ph.n] = ph; });
      return m;
    }

    function focusSet() {
      // Tour mode only: the current step's targets, for this step alone (the next render recomputes it,
      // which is what releases the previous step's highlight). Attack-path mode keeps its cumulative
      // state rendering untouched.
      if (!isTourModel(state.model)) return null;
      var ph = phaseMap()[state.p], out = {};
      ((ph && !ph.intro && ph.targets) || []).forEach(function (id) { out[id] = true; });
      return out;
    }

    function draw() {
      var g = state.g, model = state.model, style = state.style, p = state.p, tour = isTourModel(model);
      g._model = model;
      root.classList.toggle("vap-tour", tour);
      el.map.setAttribute("viewBox", g.viewBox);
      el.map.setAttribute("aria-label", tour ? "Walkthrough map" : "Attack path topology");
      el.map.style.minWidth = Math.min(1288, g.width) + "px";
      fill(el.map, [defsSvg()].concat(bandsSvg(g), edgesSvg(model, g, p, style), nodesSvg(model, g, p, style, focusSet())));
      fill(el.legend, tour ? tourLegendSvg(model, style) : legendSvg());
    }

    // Per-step image + links, built as DOM nodes (textContent / setAttribute) — never markup — and only
    // for targets that pass safeImage / safeHref.
    function appendMedia(container, ph) {
      if (!container) return;
      var src = safeImage(ph.image);
      if (src) {
        var fig = document.createElement("figure");
        fig.className = "step-media";
        var img = document.createElement("img");
        img.className = "step-img";
        img.alt = ph.title || "";
        img.loading = "lazy";
        img.setAttribute("src", src);
        fig.appendChild(img);
        container.appendChild(fig);
      }
      var links = (Array.isArray(ph.links) ? ph.links : []).filter(function (l) { return l && safeHref(l.href); });
      if (links.length) {
        var ul = document.createElement("ul");
        ul.className = "step-links";
        links.forEach(function (l) {
          var li = document.createElement("li"), a = document.createElement("a");
          a.setAttribute("href", safeHref(l.href));
          a.textContent = (typeof l.label === "string" && l.label.trim()) || safeHref(l.href);
          li.appendChild(a);
          ul.appendChild(li);
        });
        container.appendChild(ul);
      }
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

    function blk(title, kids) { return h("div", { "class": "blk" }, [h("div", { "class": "blk-h", text: title })].concat(kids)); }
    function eyebrow(word, ph) {
      return h("div", { "class": "eyebrow" }, [txt(word + " "), h("b", { text: String(ph.n).padStart(2, "0") }), txt(" / " + state.MAX)]);
    }
    function media(ph) { var box = h("div", { "data-media": "" }); appendMedia(box, ph); return box; }

    function renderBrief() {
      var model = state.model, p = state.p, pm = phaseMap();
      var ph = pm[p];
      var meta = model.meta || {};
      var tour = isTourModel(model), word = tour ? "Step" : "Phase";
      var fallbackTitle = tour ? "Walkthrough" : "Attack path";
      if (tour) el.brand.textContent = meta.title || fallbackTitle;  // no ◤ brand glyph in a tour
      else fill(el.brand, [h("b", { text: "◤" }), txt(" " + str(meta.title || fallbackTitle))]);
      el.sub.textContent = meta.subtitle || "";
      el.badge.textContent = meta.badge || "";
      el.badge.style.display = meta.badge ? "" : "none";

      if (p === 0 || (ph && ph.intro)) {
        var intro = meta.intro || {};
        fill(el.brief, [
          h("div", { "class": "eyebrow", text: intro.eyebrow || (tour ? "Guided tour" : "Walkthrough") }),
          h("div", { "class": "ph-title", text: meta.title || fallbackTitle }),
          intro.objective ? h("p", { "class": "intro-obj", text: intro.objective }) : null,
          intro.readingNotes ? blk("Reading the map", [h("p", { "class": "watch", text: intro.readingNotes })]) : null,
          intro.note ? h("div", { "class": "note", text: intro.note }) : null,
          blk("How to drive it", [h("p", { "class": "watch",
            text: "→ / Next · ← / Prev · Space auto-play · Home reset · click the progress bar to jump." })])
        ]);
        return;
      }
      if (!ph) {
        fill(el.brief, [h("div", { "class": "empty", text: word + " " + p + " — no content yet." })]);
        return;
      }
      var tacs = (ph.tactics || []).map(function (t) {
        var col = accentLine(state.style.tacticKinds[t.kind] || "slate");
        return h("span", { "class": "tac", css: { color: col, "border-color": col }, text: str(t.label) });
      });
      var tgts = (ph.targets || []).map(function (id) {
        return h("div", { "class": "tgt" }, [
          h("span", { "class": "dot", css: { background: dotColor(id) } }),
          h("span", { "class": "thn", text: nodeLabel(id) }),
          h("span", { "class": "tip", text: nodeIp(id) }),
          h("span", { "class": "tz", text: zoneTitle(id) })
        ]);
      });
      var desc = ph.desc ? h("p", { "class": "desc", text: ph.desc }) : null;
      var note = ph.note ? h("div", { "class": "note", text: ph.note }) : null;
      var watch = function (title) { return ph.watch ? blk(title, [h("p", { "class": "watch", text: ph.watch })]) : null; };
      if (tour) {
        fill(el.brief, [
          eyebrow("Step", ph),
          h("div", { "class": "ph-title", text: str(ph.title) }),
          tacs.length ? h("div", { "class": "tacs" }, tacs) : null,
          desc,
          media(ph),
          tgts.length ? blk("On the map", tgts) : null,
          watch("Look for"),
          note
        ]);
        return;
      }
      var b = ph.blue, blue;
      if (b) {
        blue = [
          h("div", { "class": b.gap ? "blue-tool gap" : "blue-tool" },
            [h("span", { text: (b.gap ? "Gap / unvalidated" : "Tool") + ":" }), txt(" " + str(b.tool || "—"))]),
          b.finding ? h("p", { "class": "desc", text: b.finding }) : null,
          b.query ? h("div", { "class": "blue-signal", text: "Example query" }) : null,
          b.query ? h("pre", { "class": "blue-query", text: b.query }) : null,
          b.seen ? h("div", { "class": "blue-seen" }, [h("b", { text: "What is seen:" }), txt(" " + str(b.seen))]) : null,
          b.note ? h("div", { "class": "blue-note " + (b.gap ? "gap-note" : "") },
            [h("b", { text: b.gap ? "Gap / caveat:" : "Notes:" }), txt(" " + str(b.note))]) : null
        ];
      } else {
        blue = [h("div", { "class": "empty", text: "No blue-team detail for this phase." })];
      }
      var tab = function (key, label) {
        return h("button", { type: "button", "class": "detail-tab" + (state.tab === key ? " active" : ""), "data-tab": key, text: label });
      };
      var pane = function (key, kids) {
        return h("div", { "class": "tab-pane" + (state.tab === key ? " active" : ""), "data-pane": key }, kids);
      };
      fill(el.brief, [
        eyebrow("Phase", ph),
        h("div", { "class": "ph-title", text: str(ph.title) }),
        h("div", { "class": "tacs" }, tacs),
        ph.mitre ? h("div", { "class": "mitre", text: ph.mitre }) : null,
        h("div", { "class": "detail-tabs", role: "tablist" }, [tab("red", "Red Team Action"), tab("blue", "Blue Team Detection")]),
        pane("red", [desc, media(ph), tgts.length ? blk("Targets this phase", tgts) : null, watch("On the map"), note]),
        pane("blue", blue)
      ]);

      root.querySelectorAll(".detail-tab").forEach(function (btn) {
        btn.addEventListener("click", function () {
          state.tab = btn.dataset.tab;
          root.querySelectorAll(".detail-tab").forEach(function (b2) { b2.classList.toggle("active", b2.dataset.tab === state.tab); });
          root.querySelectorAll(".tab-pane").forEach(function (pane) { pane.classList.toggle("active", pane.dataset.pane === state.tab); });
        });
      });
    }

    function buildRail() {
      clear(el.rail);
      // `at` has no upper bound, even after normalize(): past RAIL_MAX a rail is unreadable anyway, and one
      // DOM node per step would hang the tab. Leave it empty; Prev/Next/keys still drive the viewer.
      for (var i = 1; state.MAX <= RAIL_MAX && i <= state.MAX; i++) {
        (function (idx) {
          var seg = document.createElement("div");
          seg.className = "seg";
          seg.title = (isTourModel(state.model) ? "Step " : "Phase ") + idx;
          seg.addEventListener("click", function () { stopAuto(); go(idx); });
          el.rail.appendChild(seg);
        })(i);
      }
      var labels = state.model.meta && state.model.meta.railLabels;
      if (!Array.isArray(labels)) labels = [];  // the editor previews raw models; a non-array must not throw
      fill(el.railLabels, labels.map(function (l) { return h("span", { text: str(l) }); }));
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

    // Deep links (opts.deepLink): #step-N selects a step on mount and follows the step as it changes.
    // replaceState, not assignment, so stepping through a tour does not stack history entries. Wrapped in
    // try: a sandboxed/srcdoc iframe (scribble embeds the deliverable) may refuse history writes.
    function hashStep() {
      var m = /^#step-(\d{1,4})$/.exec((window.location && window.location.hash) || "");
      return m ? parseInt(m[1], 10) : null;
    }
    function writeHash() {
      if (!opts.deepLink) return;
      try {
        var h = "#step-" + state.p;
        if (window.location.hash !== h) window.history.replaceState(window.history.state, "", h);
      } catch (e) {}
    }
    function onHash() { var n = hashStep(); if (n !== null && n !== state.p) { stopAuto(); go(n); } }

    function go(p) {
      state.p = Math.max(0, Math.min(state.MAX, p));
      render();
      writeHash();
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
      buildRail();  // the segment count follows MAX, so a new model rebuilds it before painting
      render();
    }

    setModel(model);
    if (opts.deepLink) {
      window.addEventListener("hashchange", onHash);
      if (hashStep() !== null) go(hashStep());
      else if (opts.phase != null) go(opts.phase);
    } else if (opts.phase != null) go(opts.phase);

    return {
      setModel: setModel,
      goto: function (p) { stopAuto(); go(p); },
      phase: function () { return state.p; },
      max: function () { return state.MAX; },
      onPhaseChange: function (cb) { if (typeof cb === "function") phaseCbs.push(cb); },
      destroy: function () {
        stopAuto();
        if (opts.captureKeys) document.removeEventListener("keydown", onKey);
        if (opts.deepLink) window.removeEventListener("hashchange", onHash);
        clear(root);
      }
    };
  }

  var VectorViewer = { mount: mount, DEFAULT_STYLE: DEFAULT_STYLE, ACCENTS: ACCENTS };
  if (typeof window !== "undefined") window.VectorViewer = VectorViewer;

  // Auto-boot the deliverable.
  function bootModel() {
    if (window.__VECTOR_MODEL__) return window.__VECTOR_MODEL__;
    var island = document.getElementById("vap-model");
    if (!island) return null;
    try { return JSON.parse(island.textContent); }
    catch (e) { return { schema: "vector.attackpath/v1", meta: { title: "Parse error" }, zones: [], nodes: [], edges: [], phases: [] }; }
  }
  function boot() {
    if (typeof window === "undefined") return;
    var model = bootModel();
    if (!model) return;
    var host = document.getElementById("vap") || document.body;
    var inst = mount(host, model, { captureKeys: true, deepLink: true });
    // On paper, an animated walkthrough is whatever phase it happened to be on — usually the intro,
    // i.e. an empty diagram (ext#115). Print the FINAL keyframe instead: the whole path, every node in
    // its end state. `goto` also stops the auto-play timer, so the printed frame cannot move under the
    // print engine. Restoring the phase afterwards keeps the on-screen walkthrough where the reader
    // left it — the phase, not the auto-play state, which `goto` stops and nothing restarts.
    //
    // It fires for the deliverable embedded in scribble's report iframe too, with one caveat that is
    // NOT handled here: that iframe is `loading="lazy"`, so a diagram still below the fold when the
    // parent prints may never have booted, and then no listener exists to fire. Print the report with
    // the diagram on screen, or use the toolbar's Print button after scrolling to it.
    if (typeof window.addEventListener === "function") {
      var resume = null;
      window.addEventListener("beforeprint", function () {
        // Only the FIRST of a run captures. Chrome fires beforeprint per window.print(), so two prints
        // without an intervening afterprint would otherwise overwrite `resume` with max() and
        // "restore" the reader to the end of the walkthrough, permanently.
        if (resume === null) resume = inst.phase();
        inst.goto(inst.max());
      });
      window.addEventListener("afterprint", function () {
        if (resume !== null) { inst.goto(resume); resume = null; }
      });
    }
  }
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})();
