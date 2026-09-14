# Plan: feat/kit-shared-reporting-editor (+ follow-on phases)

- **Branch (P1):** `feat/kit-shared-reporting-editor` (off `main`)
- **Status:** 🟡 P1 in progress — plan landed, kit primitive being extracted
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
