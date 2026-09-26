/* Report-prose editor: "Reset to standard" clears a textarea (Save then stores NULL = the standing text);
 * "Rephrase with AI" sends the draft to the host AI hook and drops the rewrite back into the textarea for
 * the operator to review before Save (never a silent overwrite). CSP-strict: external classic script, no
 * inline handlers. Degrades to a plain edit/save form if this file or the AI hook is absent. */
(function () {
  "use strict";
  var form = document.querySelector("[data-prose-form]");
  if (!form) return;
  var rephraseUrl = form.getAttribute("data-rephrase-url");
  var status = form.querySelector("[data-prose-status]");

  function setStatus(msg) {
    if (status) status.textContent = msg || "";
  }

  form.addEventListener("click", function (ev) {
    var reset = ev.target.closest ? ev.target.closest("[data-reset]") : null;
    if (reset) {
      var target = document.getElementById(reset.getAttribute("data-reset"));
      if (target) target.value = "";
      setStatus("Cleared — Save to use the standard text.");
      return;
    }
    var button = ev.target.closest ? ev.target.closest("[data-rephrase]") : null;
    if (!button || !rephraseUrl) return;
    var ta = document.getElementById(button.getAttribute("data-rephrase"));
    if (!ta || !ta.value.trim()) {
      setStatus("Nothing to rephrase.");
      return;
    }
    button.disabled = true;
    setStatus("Rephrasing…");
    fetch(rephraseUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ field: ta.getAttribute("data-field"), text: ta.value }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (res.ok && res.j && res.j.text) {
          ta.value = res.j.text;
          setStatus("Rephrased — review it, then Save.");
        } else {
          setStatus((res.j && res.j.detail) || "Rephrase failed.");
        }
      })
      .catch(function () { setStatus("Rephrase failed."); })
      .then(function () { button.disabled = false; });
  });
})();
