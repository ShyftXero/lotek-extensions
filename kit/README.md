# lotek-kit

**This is not an extension.** It is a shared contract library that lotek core and lotek extensions both
depend on, so that neither has to depend on the other.

## Why it exists

The platform's rule is that an extension reaches lotek only through the injected host contract, never by
importing it. Core reaching the other way is worse — and core does exactly that today
(`lotek:src/app/routes/jobs.py` imports `vector.models` and writes an extension's table). Two consumers
needing the same code had nowhere neutral to put it, so it got copied instead: the repo currently carries
**three** hand-written native-HTML5 drag-reorder implementations, and the `attackpath/v1` document schema
is known by core as a hardcoded string literal.

`lotek-kit` is that neutral ground. Core depends on it; extensions depend on it; it depends on neither.

## What is in it

| Module | Purpose |
| --- | --- |
| `lotek_kit.attackpath` | The `attackpath/v1` document model — `normalize()`, `blank_model()`, `is_supported_schema_id()`. Ported from `vector/vector/schema.py`. |
| `lotek_kit.assets` | Stdlib access to shipped browser assets, for inlining into a self-contained deliverable. |
| `lotek_kit.static/` | The browser assets themselves — `reorder.{js,css}`, and the shared **reporting editor** primitive `reporting-editor.{js,css}` + `reporting-outbox.js` (a `contenteditable` ProseMirror-JSON editor with image paste→upload; extension-neutral — the host supplies its own `apiBase` (image upload POSTs to `apiBase + "/artifacts"`) and mounts via `window.LotekReportingEditor.mount`). |
| `lotek_kit.static/vector-viewer.{js,css}` | A **byte-identical copy** of vector's attack-path / tour viewer, so a public page can load it from `/_kit/` without a login (everything under `/vector/*` is authenticated). Fix vector's copy, then `cp` it here — `tests/test_viewer_parity.py` fails on any drift. |

## Embedding a tour on a public page

Core serves this package's `static/` at `/_kit/` with no login, so a public page (core's `/docs`, the
landing page) embeds a vector tour with two same-origin files and a JSON data block:

```html
<link rel="stylesheet" href="/_kit/vector-viewer.css">
...
<div id="vap"></div>
<script type="application/json" id="vap-model">{"schema":"attackpath/v1","meta":{"mode":"tour", ...}, ...}</script>
<script src="/_kit/vector-viewer.js"></script>
```

In a Jinja template, prefer `url_for("lotek_kit.static", filename="vector-viewer.js")` over the literal
path — it resolves under whatever prefix the kit was registered at (see `lotek_kit.flask_assets`).

- **No inline script, no inline style, no iframe.** The runtime boots itself: on `DOMContentLoaded` it
  parses `#vap-model` and mounts into `#vap` (or `<body>` if there is none). Colours reach the SVG
  through the CSSOM, not `style=` attributes, so the page works under
  `Content-Security-Policy: script-src 'self'; style-src 'self'`. vector's Playwright suite
  (`vector/tests/test_e2e_tour.py`) asserts zero violations under exactly that policy.
- **The JSON block must be escaped for a `<script>` element.** A `</script>` inside a string would
  close the element early. Escape `<`, `>`, `&` and U+2028/U+2029 — `vector.render.json_for_script`
  does exactly this; core should do the same rather than `json.dumps` alone.
- **Normalize the model first** (`lotek_kit.attackpath.normalize`). The runtime never parses a model
  string as HTML (every element is built as a DOM node; text goes in as text) and re-narrows colours,
  numbers and URLs on its own, but a normalized document is the one
  the caps were designed for. (A model whose step numbers run past 200 gets no progress rail.)
- **`/_kit/` is the whole of `lotek_kit/static/`, public.** Anything added to that directory is served
  without a login, so it must never hold anything but browser assets.
- **What the runtime refuses.** A step `image` must be same-origin or a base64 `data:image/...` URI; a
  step `links[].href` must be same-origin, and an absolute path must sit under `/docs`. Anything else is
  dropped silently rather than rendered.
- **One viewer per page.** The auto-boot reads one `#vap-model`, captures the arrow / Space / Home keys,
  and keeps `#step-N` in the URL hash (via `replaceState`, so stepping does not add history entries).

## The admission rule

Something belongs in the kit when **two consumers that may not import each other both need it**. That is
the whole test. A helper only scribble uses belongs in scribble; convenience is not a reason to widen a
package that every consumer is forced to install.

Three hard constraints, each pinned by a test in `tests/`:

1. **No runtime dependencies.** Core takes this as a *base* dependency, so anything added here lands in
   lotek itself and in every extension downstream. Flask is an optional extra, imported inside the
   function that needs it.
2. **It never imports lotek or an extension.** Not at module scope, not lazily.
3. **It cannot become an extension.** No `lotek-extension.toml`, no `lotek.extensions` entry point, no
   `register()`. lotek's discovery enumerates exactly that entry-point group, so their absence is what
   makes this package structurally unmountable rather than merely unmounted.

## Versioning

There is no `version` literal. Every other subproject in this monorepo is frozen at `0.1.0.dev0` with the
real version derived from the git build id, so a hand-bumped semver here would be the only one and would
drift the first time someone forgot it. `hatch_build.py` stamps the same dated
`YYYY.M.D.HHMMSS+g<shorthash>` string `scripts/build_id.py` produces, and a test asserts the two agree.

## How consumers pin it

```toml
# an extension, or anything else in this monorepo
dependencies = ["lotek-kit"]
[tool.uv.sources]
lotek-kit = { path = "../kit", editable = true }
```

`editable = true` is load-bearing, not cosmetic — a non-editable path install is cached by version, so
after editing kit source `uv sync` reports "Audited" and does not rebuild (the trap documented in the
repo's `CLAUDE.md`). Because the kit ships no force-included files, editable is safe here in a way it is
not for an extension.

lotek core pins it as a git dependency on a release tag, in its **base** dependencies rather than the
`extensions` extra — core's PR-gate CI lane runs `uv sync --extra dev` and installs no extensions, so a
kit in that extra would be absent on the lane that gates every PR.

**Core's tag and every kit-consuming extension's tag must match.** One process has exactly one
`lotek_kit` module, so two consumers on different kits is a bug rather than a configuration. If they
diverge, `uv lock` fails loudly with `Requirements contain conflicting URLs for package 'lotek-kit'`
naming both tags — the disagreement cannot reach runtime.

## What is deliberately absent

**A version-guard helper.** The obvious design — `require("x.y")` called from an extension's
`register()` — is a security hole, not a safety net. By the time `register()` can raise,
`app.register_blueprint` has already run and is irreversible; the host's `_inject_host` (the only writer
of the authorization extras) has *not* yet run; and `mount_extensions` catches the exception and carries
on. That leaves a mounted surface with no injected authorization. Skew is prevented by the pin recipe
above instead, where it fails at lock time rather than at request time.
