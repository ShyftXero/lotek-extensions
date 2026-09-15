# Plan: feat/kit-shared-reporting-editor (+ follow-on phases)

- **Branch (P1):** `feat/kit-shared-reporting-editor` (off `main`)
- **Branch (P3):** `feat/scribble-adopt-kit-editor` (off `main`)
- **Status:** 🟡 P1 + P2 SHIPPED · P3 in review (scribble adopts the primitive; NOT live-validated) · P4/P5 not started
- **Directive (Eli, 2026-09-14):** screenshots via ctrl+v; images in blob storage; purge deletes images;
  an orphan sweep reaps bucket objects with no valid id; **and the rich reporting editor is a SHARED
  PRIMITIVE — one canonical copy, updated in one place, that extensions load and CONFIGURE / OVERRIDE
  (custom nodes, upload endpoint, or opt out), for a consistent UX. Not copy-pasted per extension.**

## Architecture

scribble already owns a hand-rolled, ProseMirror-shaped contentEditable editor with working ctrl+v
image-paste → blob upload (`scribble/static/{editor,outbox}.js`, mounted via `_editor.html`). Its
`mount(container, options)` is already parameterized (`apiBase`, `artifactUrl`, `initialDoc`, `block`,
`variableKeys`) and carries a TipTap drop-in seam (`window.ScribbleTipTap`). It is NOT a shared asset —
it lives in scribble's package and can't be reached by another extension (extensions must not import each
other).

The kit is the sanctioned shared home: `lotek_kit/static/` already ships browser assets (`reorder.js`),
served by a `lotek_kit.static` blueprint (`flask_assets.ensure_registered`), with the stable template
contract `url_for("lotek_kit.static", filename=...)` whether mounted in core (`/_kit`) or an extension.
The kit's admission rule — "something enters the kit only when two consumers that may not import each
other both need it" — is now met (scribble + bugreport). The machinery exists but is **dormant** (no host
calls `ensure_registered` today).

**The shared primitive + override model:**
- `lotek_kit/static/reporting-editor.js` (+ `reporting-outbox.js`, `reporting-editor.css`) — the ONE
  canonical editor. Kit-neutral global (`window.LotekReportingEditor`), the durable upload outbox, and a
  `window.LotekReportingEditorTipTap` drop-in seam preserved.
- Config (per `mount`): `uploadUrl` (REQUIRED — no scribble default), `uploadResponse` adapter
  (map the extension's upload response → `{id, url}`), `initialDoc`, `blocks`, `user`, and `plugins` —
  the override point. A plugin registers extra nodes/marks/toolbar items. **scribble's `{{variable}}`
  chip becomes a scribble-supplied plugin, not baked into the kit** — the "override / do their own thing"
  the directive asks for. An extension may also decline the primitive entirely.
- Core wires `ensure_registered(app)` once at boot so `/_kit/*` serves the assets for every mounted
  extension; the host base may link the shared CSS.

## Phases (each a PR; cross-repo re-pin between the ext repo and core)

- **P1 — ext(kit): the shared primitive.** Move `editor.js`/`outbox.js`/editor-CSS into
  `lotek_kit/static/`, generalized: kit-neutral globals, `uploadUrl`+`uploadResponse` config (drop the
  `/scribble/api` default), the `{{variable}}` node extracted to an opt-in plugin, TipTap seam kept.
  Kit tests: the assets exist, `mount`/`_internal`/the plugin registry are exported, a plugin can add a
  node. Additive — scribble/bugreport not yet switched. **← this branch.**
- **P2 — core: serve it.** Call `lotek_kit.flask_assets.ensure_registered(app)` at boot; re-pin kit to
  P1's tag. Mounted test: `url_for("lotek_kit.static", filename="reporting-editor.js")` → 200.
- **P3 — ext(scribble): adopt the primitive.** `_editor.html` loads the editor from `lotek_kit.static`
  and registers scribble's `{{variable}}` plugin; **delete scribble's own `editor.js`/`outbox.js`** (one
  home). Byte-identical behavior — proven by scribble's suite + a live Firefox paste/save check. Re-pin
  scribble in core.
- **P4 — ext(bugreport): paste screenshots as ATTACHMENTS (Eli 2026-09-14: "use attachments").** NOT a
  rich `content_json` body. bugreport keeps its plain textarea; a small SHARED **paste-upload helper**
  from the kit (the outbox + a `attachPasteUpload(el, {uploadUrl, onUploaded})` — the paste handler the
  editor also uses internally) is wired to the body textarea. Ctrl+V a screenshot → uploads to bugreport's
  existing attachment endpoint → stored in `ExtensionBlobs` → appears in the report's Files list. Fix
  `delete_own` to also delete the report's blobs (closes the leak at `service.py:173`). A small schema
  touch is acceptable if needed (e.g. flag a pasted attachment), but no rich-body migration. Re-pin in core.
- **P5 — core: an ADMIN MAINTENANCE UI, human-driven (NOT the cut auto-sweep).** (Eli 2026-09-14.) The
  auto-`reconcile_extension_blobs` stays cut — its unattended data-loss is exactly the risk. Instead:
  each extension owns cleanup of orphans in ITS OWN prefix, surfaced to an admin. Build (a)
  `ExtensionBlobs.list_ids` (paginated S3 enumeration of `ext/<name>/*` — the missing piece), (b) an
  orphan check = enumerated ids minus the extension's `claimed_blob_ids` (the manifest hook that already
  exists, revived read-side), (c) an admin maintenance page: list orphaned objects per extension with
  checkboxes, and per-selection **download-to-save** or **delete**. Human-in-the-loop → no unattended
  purge; the safety net so opaque-S3 files can't accumulate invisibly. Its own branch; deletion still
  gets an "are you sure" + audit. NOT part of P1→P4.

## Notes / risks

- **Cross-repo round-trips:** kit→(core re-pin+serve)→scribble→(core re-pin)→bugreport→(core re-pin), then
  P5 in core. Each ext PR auto-cuts a release tag; core re-pins the one-line `tag=`.
- **P5 is the only data-loss-capable piece** and was cut on adversarial review + owner decision (#492 /
  `feat-extension-blob-seam.md:123`). Rebuild it deliberately with its earned guards + invariant +
  independent "refute this claim" review — never rushed alongside the editor work.
- **`{{variable}}` stays OUT of the kit core** — scribble-specific; it's the reference override plugin.
  scribble's `{{VARIABLE}}` templating (built-ins `COMPANY_NAME`, `ENGAGEMENT_NAME`, `TODAY`,
  `START_DATE`, `END_DATE`, `TARGET_HOST`, `TARGET_PORT`, `TARGET_URL`, `ASSESSOR`, `SEVERITY` + custom
  per-engagement vars; resolved through a SandboxedEnvironment) is KEPT and is precisely the reference
  override plugin the kit editor loads via config. The refactor must not drop it. (Eli 2026-09-14:
  "the jinja variables are what make the report feel tailored.")
- **P3 rider — surface available variables as a visible reminder.** scribble already offers them via the
  editor's variable picker + the resolve-preview; Eli wants them OFFERED more plainly as a reminder. Add
  an always-visible "Available variables" hint/list beside the editor (driven by `known_variable_keys`),
  and consider an `OPERATOR_HOST`/tester-identity built-in in `templating/resolver.py::build_context`
  (today the closest is `ASSESSOR` = engagement.created_by; there is no operator-host var). Small, scribble-side.
- **bugreport `content_json`** is a schema change + a render/sanitize surface. Reuse scribble's
  `prosemirror_sanitize.py` — itself a kit-share candidate once two consumers need it (P4 will decide:
  share it, or bugreport carries its own until a third consumer appears).
- **Consistency drift-guard:** a test asserting no extension ships its OWN editor copy once the kit owns
  it (the kit README already laments three hand-rolled drag-reorder impls — don't add an editor to that
  list).

## Evals (EDD)

- **P1:** kit test — mount API + plugin seam exported; a headless JS smoke that `mount` builds an editable
  surface and a registered plugin's node round-trips through the JSON serializer.
- **P3:** scribble live (Firefox) — paste an image, it uploads + saves; `content_json` byte-identical to
  the pre-move baseline for the same input.
- **P4:** bugreport live (Firefox) — ctrl+v inline screenshot uploads to `ExtensionBlobs`, renders on the
  report, survives save; deleting the report deletes the blob (assert the key is gone).
- **P5:** its own baseline — a planted orphan is reaped; a blob with a live row is NEVER deleted; a broken
  claims function (empty/wrong-typed) reaps nothing.

---

## P3 — scribble adopts the primitive (`feat/scribble-adopt-kit-editor`)

**Purpose.** Make scribble load the ONE canonical editor from the kit and delete its own copy, so the
"updated in one place" promise in the directive above is a fact about the tree rather than an intention.

### Done

- `_editor.html` mounts `window.LotekReportingEditor` from `lotek_kit.static` (`reporting-outbox.js`
  before `reporting-editor.js`; the editor reads the outbox global at script-load time), links
  `reporting-editor.css`, and drops its inline `<style>`. Container class is
  `lotek-reporting-editor-wrap` (the kit CSS's selector); `data-scribble-editor` stays as the MOUNT
  SELECTOR — that attribute is scribble's page contract, not the editor's. Embedded doc/vars script
  classes renamed to what the kit's `readEmbeddedDoc`/`readVariableKeys` actually query
  (`lotek-reporting-editor-doc-data` / `-vars-data`). `draft.js` and the `data-scribble-rephrase` block
  are scribble's own and stay.
- **Variable reminder** (the "P3 rider" above): a visible muted `Variables: {{…}} …` line beside the
  editor whenever `scribble_variable_keys` is non-empty, so the operator sees them without opening the
  picker.
- `_gallery.html` loads `reporting-outbox.js`; `artifacts.js` calls `window.LotekReportingOutbox`.
- `library_detail.html` loads `reporting-editor.js`. It uses only the exported `_internal` JSON<->DOM
  walkers (never `mount()`, never image upload), so it deliberately does NOT also load the outbox — the
  editor's two outbox touchpoints are both guarded (`uploadAndInsertImage` early-returns with a status
  message; the resolved/failed wiring is `if (window.LotekReportingOutbox && …)`), and an unused outbox
  would open an IndexedDB connection on a page that never uploads.
- Deleted `scribble/static/editor.js` and `scribble/static/outbox.js`.
- Drift guard `tests/test_kit_editor_adoption.py`: scribble ships no editor/outbox copy, no
  `window.Scribble{Editor,Outbox}` reference survives, and every template that mounts the editor loads
  the kit assets in the right order. This is the "consistency drift-guard" the Notes section asks for.

### The test-harness shim (this is what wedged the first attempt)

`url_for('lotek_kit.static', …)` only resolves in a host that registered the kit blueprint. Core does
(P2, `/_kit`). Scribble's own suite boots a bare `Flask()`, so every template render raised
`BuildError` and the suite died. `scribble.testing.register_kit_assets_shim(app)` registers a blueprint
named `lotek_kit` serving the monorepo's real `kit/lotek_kit/static/` — the same bytes core serves, so
the Playwright suites still exercise the actual editor rather than a 404. It is called from conftest's
autouse fixture and from the two e2e `live_app` builders. Red-then-green transcript in the PR.

**Assumption, recorded:** scribble now has a TEMPLATE-level dependency on a host that calls
`lotek_kit.flask_assets.ensure_registered(app)`, but no package-level dependency on `lotek-kit`. That is
deliberate — adding one would be this monorepo's first `[tool.uv.sources]` path dep and a lockfile
change, for no runtime gain, since core already registers the blueprint. The cost is that a host which
mounts scribble WITHOUT the kit gets a `BuildError` on the finding page. If a second consumer ever needs
this, promote the shim to a real `ensure_registered` call in `scribble.register()`.

### Remaining (NOT done on this branch — needs a session with core + a browser)

- MOUNTED/live validation: boot core, load a finding page, confirm the editor mounts from `/_kit`,
  paste a screenshot → `POST /scribble/api/artifacts` → inline image, autosave →
  `/scribble/api/findings/<id>/blocks/<block>`. Firefox.
- Re-pin scribble's tag in core's `pyproject.toml` `[tool.uv.sources]` + `uv lock` + run
  `tests/test_scribble_*` mounted; open the core re-pin PR.

### Notes / gotchas found while building

- The kit editor's `apiBase` has **no default** (P1 dropped `/scribble/api`). Scribble's template always
  emits `data-api-base`, so this is inert — but any new scribble surface that mounts the editor must
  pass it or every autosave/upload POSTs to a relative `/artifacts`.
- The kit outbox renamed its IndexedDB store (`scribble-outbox` → `lotek-reporting-outbox`) and its test
  config global (`__scribbleOutboxConfig` → `__lotekReportingOutboxConfig`). Consequence for a live
  deploy: uploads still queued in a browser's OLD database at upgrade time are orphaned — they are not
  lost from disk, but nothing drains them. Judged acceptable (the window is one page-load wide and the
  queue is normally empty); noting it rather than pretending it is nothing.
- P1 shipped the `{{variable}}` chip as an **opt-in via `variableKeys`**, not as the separate plugin
  registry this plan's P1 bullet described. Adoption needs no plugin registration as a result — the
  template's `data`-embedded vars list is enough. The plan's earlier wording is the stale one.
