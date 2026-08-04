/* Checklist library admin: list / create / import (markdown|JSON) / edit / hide / reset / duplicate /
 * export, all against the checklist JSON API. */
(function () {
  "use strict";
  var root = document.getElementById("checklist-library");
  if (!root) return;
  var API = (root.dataset.templatesUrl || "").replace(/\/checklists\/templates$/, "");
  var listEl = document.getElementById("cklib-list");
  var showHidden = document.getElementById("cklib-hidden");

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

  function actionBtn(label, fn) {
    var b = el("button", "btn ghost cklib-act", label);
    b.addEventListener("click", fn);
    return b;
  }
  function exportLink(t, fmt, label) {
    var a = el("a", "btn ghost cklib-act", label);
    a.href = API + "/checklists/templates/" + t.id + "/export?format=" + fmt;
    a.setAttribute("download", "");
    return a;
  }

  function card(t) {
    var c = el("div", "cklib-card card" + (t.hidden ? " is-hidden" : ""));
    var head = el("div", "cklib-card-head");
    head.appendChild(el("strong", null, t.name));
    head.appendChild(el("span", "ckp-kind", t.kind));
    if (t.builtin) head.appendChild(el("span", "cklib-tag", "builtin"));
    if (t.customized) head.appendChild(el("span", "cklib-tag", "modified"));
    if (t.hidden) head.appendChild(el("span", "cklib-tag", "hidden"));
    head.appendChild(el("span", "muted cklib-count", t.item_count + " items"));
    c.appendChild(head);

    var acts = el("div", "cklib-actions");
    acts.appendChild(actionBtn("Rename", function () {
      var name = window.prompt("Name:", t.name);
      if (name) jpost(API + "/checklists/templates/" + t.id, { name: name }).then(load);
    }));
    acts.appendChild(actionBtn("Kind", function () {
      var kind = window.prompt("Kind (coverage / reminder / compliance):", t.kind);
      if (kind) jpost(API + "/checklists/templates/" + t.id, { kind: kind }).then(load);
    }));
    acts.appendChild(actionBtn(t.hidden ? "Unhide" : "Hide", function () {
      jpost(API + "/checklists/templates/" + t.id + "/hide", { hidden: !t.hidden }).then(load);
    }));
    if (t.builtin && t.customized) {
      acts.appendChild(actionBtn("Reset", function () {
        if (window.confirm("Reset this builtin to its shipped default?")) {
          jpost(API + "/checklists/templates/" + t.id + "/reset").then(load);
        }
      }));
    }
    acts.appendChild(actionBtn("Duplicate", function () {
      jpost(API + "/checklists/templates/" + t.id + "/duplicate").then(load);
    }));
    acts.appendChild(exportLink(t, "json", "Export JSON"));
    acts.appendChild(exportLink(t, "md", "Export MD"));
    c.appendChild(acts);
    return c;
  }

  function load() {
    var url = API + "/checklists/templates" + (showHidden.checked ? "?hidden=1" : "");
    jget(url).then(function (d) {
      listEl.innerHTML = "";
      var rows = (d && d.templates) || [];
      if (!rows.length) { listEl.appendChild(el("p", "muted", "No checklists.")); return; }
      rows.forEach(function (t) { listEl.appendChild(card(t)); });
    });
  }

  // create / import form
  var form = document.getElementById("cklib-form");
  var errEl = document.getElementById("cklib-err");
  document.getElementById("cklib-new").addEventListener("click", function () { form.hidden = !form.hidden; });
  document.getElementById("cklib-cancel").addEventListener("click", function () { form.hidden = true; });
  document.getElementById("cklib-save").addEventListener("click", function () {
    errEl.textContent = "";
    var fmt = document.getElementById("cklib-fmt").value;
    var src = document.getElementById("cklib-src").value;
    var name = document.getElementById("cklib-name").value;
    var kind = document.getElementById("cklib-kind").value;
    var payload;
    if (fmt === "json") {
      var parsed;
      try { parsed = JSON.parse(src); } catch (e) { errEl.textContent = "Invalid JSON"; return; }
      payload = { template: parsed };
      if (name) payload.template.name = name;
      if (kind) payload.template.kind = kind;
    } else {
      payload = { markdown: src, kind: kind };
      if (name) payload.name = name;
    }
    jpost(API + "/checklists/templates", payload).then(function (d) {
      if (d && d.ok) {
        form.hidden = true;
        document.getElementById("cklib-src").value = "";
        document.getElementById("cklib-name").value = "";
        load();
      } else {
        errEl.textContent = (d && d.error) || "Create failed";
      }
    });
  });

  showHidden.addEventListener("change", load);
  load();
})();
