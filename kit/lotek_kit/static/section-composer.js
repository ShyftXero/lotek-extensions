/* lotek-kit — section composer: a draggable "toolbox of modules" for composing an ordered, on/off list.
 *
 * Drawn from core's scan-composer UX (a palette of draggable module tiles) but generic: it composes ANY
 * ordered + toggleable list of items. Scribble's report-layout editor is the first consumer; core's scan
 * composer is the intended second (it can adopt this for its step reordering + on/off, keeping its richer
 * per-module params/parallel controls on top) — two consumers that may not import each other, which is
 * exactly why this lives in the kit rather than in either. It is the higher-level widget layer over
 * `reorder.js` (the pure DnD primitive): reorder.js moves keys, this renders the tiles, owns the on/off
 * toggle + presets, and persists.
 *
 * No external library (CSP-strict, no CDN) and no framework — a classic script exposing one frozen global
 * plus a DOMContentLoaded auto-mount, exactly like reorder.js. Accessibility is not optional: every tile
 * carries reorder.js `arrows()` (keyboard/touch) beside the drag handle, since HTML5 drag fires on neither.
 *
 * Markup contract (auto-mounted for every `[data-section-composer]`):
 *   <ul data-section-composer data-save-url="/…">
 *     <li class="sc-item" draggable="true" data-key="summary">
 *       <span class="sc-grip">⠿</span>
 *       <label class="sc-item-enable"><input type="checkbox" class="sc-item-toggle" checked></label>
 *       <span class="sc-item-name">Executive Summary</span>
 *       <span class="sc-item-arrows"></span>
 *     </li>
 *     …
 *   </ul>
 * Presets: any `[data-preset][data-keys="a,b,c"]` button in the same enclosing section applies that order
 * (those keys enabled, in order; the rest disabled at the end).
 *
 * Persistence: on any change it POSTs `{order:[{key,enabled}, …]}` (JSON) to `data-save-url` and also
 * dispatches a cancelable `section-composer:change` CustomEvent (detail:{order}) so a live preview can hook
 * in. The caller owns the route + auth; this only knows the URL. (`data-save-url` omitted → no fetch, event
 * only — the programmatic/preview mode.)
 */
(function (global) {
  "use strict";

  var doc = global.document;

  function itemsIn(container) {
    return Array.prototype.slice.call(container.querySelectorAll(".sc-item"));
  }

  /** The current order + on/off state, in DOM order: [{key, enabled}, …]. */
  function readOrder(container) {
    return itemsIn(container).map(function (li) {
      var box = li.querySelector(".sc-item-toggle");
      return { key: li.getAttribute("data-key"), enabled: !!(box && box.checked) };
    });
  }

  /* Attribute-selector-safe key. Keys here are block slugs ([a-z_]), but be defensive. */
  function cssEscape(value) {
    if (global.CSS && global.CSS.escape) return global.CSS.escape(value);
    return String(value).replace(/["\\\]]/g, "\\$&");
  }

  /** Move the tiles in `container` into `keys` order (a permutation of the existing keys). */
  function applyOrder(container, keys) {
    keys.forEach(function (key) {
      var li = container.querySelector('.sc-item[data-key="' + cssEscape(key) + '"]');
      if (li) container.appendChild(li); // appendChild MOVES an existing child — no clone, listeners kept
    });
  }

  function persist(container) {
    var order = readOrder(container);
    var event = new global.CustomEvent("section-composer:change", { detail: { order: order }, cancelable: true });
    var proceed = container.dispatchEvent(event);
    var url = container.getAttribute("data-save-url");
    if (!proceed || !url) return; // preview-only, or a listener cancelled the save
    try {
      global.fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // same-origin so the session cookie rides along, exactly like scribble's board.js reorder POST
        credentials: "same-origin",
        body: JSON.stringify({ order: order }),
      }).catch(function () {});
    } catch (ignored) {}
  }

  /** Reflect a tile's on/off state into its class so CSS can grey a disabled ("in the toolbox") tile. */
  function reflectEnabled(li) {
    var box = li.querySelector(".sc-item-toggle");
    li.classList.toggle("sc-item-off", !(box && box.checked));
  }

  /** Rebuild the per-tile keyboard arrow buttons (indices shift after every move). */
  function renderArrows(container) {
    var items = itemsIn(container);
    items.forEach(function (li, index) {
      var slot = li.querySelector(".sc-item-arrows");
      if (!slot || !global.lotekReorder) return;
      slot.textContent = "";
      slot.appendChild(global.lotekReorder.arrows({
        index: index,
        length: items.length,
        labelUp: "Move up",
        labelDown: "Move down",
        onMove: function (delta) {
          var order = global.lotekReorder.moveItem(
            items.map(function (el) { return el.getAttribute("data-key"); }), index, delta);
          applyOrder(container, order);
          renderArrows(container);
          persist(container);
        },
      }));
    });
  }

  /** Apply a preset's full arrangement: ``specs`` is ``[{key, enabled}, …]``. Its keys are ordered first
   * (with the stored enabled state), everything else follows DISABLED — so a preset carries both order AND
   * visibility. A built-in layout is just its blocks all-enabled; a saved preset is the operator's exact
   * arrangement. */
  function applySpecs(container, specs) {
    var enabledByKey = {};
    var wanted = [];
    (specs || []).forEach(function (s) {
      if (s && s.key && wanted.indexOf(s.key) === -1) {
        enabledByKey[s.key] = !!s.enabled;
        wanted.push(s.key);
      }
    });
    var present = itemsIn(container).map(function (el) { return el.getAttribute("data-key"); });
    var rest = present.filter(function (k) { return wanted.indexOf(k) === -1; });
    applyOrder(container, wanted.concat(rest));
    itemsIn(container).forEach(function (li) {
      var key = li.getAttribute("data-key");
      var on = Object.prototype.hasOwnProperty.call(enabledByKey, key) ? enabledByKey[key] : false;
      var box = li.querySelector(".sc-item-toggle");
      if (box) box.checked = on;
      reflectEnabled(li);
    });
    renderArrows(container);
    persist(container);
  }

  function wireControls(container) {
    // The combobox + save button sit outside the <ul>, in the enclosing section/details.
    var scope = container.closest("section, form, details, div") || doc;
    var status = scope.querySelector("[data-preset-status]");
    function setStatus(msg) { if (status) status.textContent = msg || ""; }

    var select = scope.querySelector("[data-preset-select]");
    if (select && !select.__scWired) {
      select.__scWired = true;
      select.addEventListener("change", function () {
        var opt = select.options[select.selectedIndex];
        var raw = opt && opt.getAttribute("data-specs");
        if (raw) {
          try { applySpecs(container, JSON.parse(raw)); } catch (ignored) {}
        }
        select.selectedIndex = 0; // the preset was applied; return the combobox to its placeholder
      });
    }

    var saveBtn = scope.querySelector("[data-save-preset]");
    var saveUrl = container.getAttribute("data-save-preset-url");
    if (saveBtn && saveUrl && !saveBtn.__scWired) {
      saveBtn.__scWired = true;
      saveBtn.addEventListener("click", function () {
        var name = (global.prompt("Name this section sequence:") || "").trim();
        if (!name) return;
        saveBtn.disabled = true;
        setStatus("Saving…");
        global.fetch(saveUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ name: name, order: readOrder(container) }),
        })
          .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
          .then(function (res) {
            if (res.status === 200) {
              setStatus("Saved.");
              global.location.reload(); // the new preset joins the combobox on reload
            } else {
              setStatus((res.j && (res.j.detail || res.j.error)) || "Save failed.");
            }
          })
          .catch(function () { setStatus("Save failed."); })
          .then(function () { saveBtn.disabled = false; });
      });
    }
  }

  /** Wire one composer container. Idempotent. */
  function mount(container) {
    if (!container || container.__scMounted) return;
    container.__scMounted = true;

    if (global.lotekReorder) {
      global.lotekReorder.attach(container, {
        itemSelector: ".sc-item",
        keyAttr: "data-key",
        onMove: function (result) {
          applyOrder(container, result.order); // reorder.js reports the order; the widget moves the DOM
          renderArrows(container);
          persist(container);
        },
      });
    }

    container.addEventListener("change", function (event) {
      var box = event.target.closest ? event.target.closest(".sc-item-toggle") : null;
      if (!box) return;
      var li = box.closest(".sc-item");
      if (li) reflectEnabled(li);
      persist(container);
    });

    itemsIn(container).forEach(reflectEnabled);
    renderArrows(container);
    wireControls(container);
  }

  function mountAll(root) {
    var scope = root || doc;
    if (!scope) return;
    Array.prototype.forEach.call(scope.querySelectorAll("[data-section-composer]"), mount);
  }

  if (doc) {
    if (doc.readyState === "loading") {
      doc.addEventListener("DOMContentLoaded", function () { mountAll(); });
    } else {
      mountAll();
    }
  }

  var api = { mount: mount, mountAll: mountAll, readOrder: readOrder, applySpecs: applySpecs };
  if (Object.freeze) Object.freeze(api);
  global.lotekSectionComposer = api;
})(this);
