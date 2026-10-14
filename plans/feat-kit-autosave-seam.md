# feat: unified autosave seam in the kit (doc-handle registry)

- **Status:** phase 1 in progress

## Purpose
Collapse the N bespoke autosave backends into ONE kit seam so every editor surface (scribble findings,
scribble library templates, vector diagrams, future scribble engagement-prose) saves through a single
endpoint, and adding autosave to a new surface is "register a kind", not "write a route". Kills the
route-drift class and reduces duplicative code. Directed by Eli (2026-10-14).

## The problem, measured
The kit reporting-editor (`kit/lotek_kit/static/reporting-editor.js`) is already shared, and already has a
pluggable `save` hook + `apiBase` — but the autosave CONVENTION is `findingId`-keyed, so every surface
ships its OWN backend:

| Surface | Bespoke backend today | Storage |
|---|---|---|
| scribble findings (cookie) | `scribble/autosave_api.py` `POST/GET /findings/<id>/blocks/<block>` | `BoardFinding.content_json`/`content_html` |
| scribble findings (machine) | `scribble/api_pat.py` content_json writer (the PAT twin) | same |
| scribble library templates | `scribble/library_ui.py` | library template content |
| vector diagrams | `vector/vector/api_pat.py` + browser `<diagram_id>` routes | `Diagram` |

Four writers of "a ProseMirror doc into a column + cache rendered HTML", each with its own authz, its own
route, free to drift (the finding-preview render-path drift we just fixed was the same disease one layer
up).

## Design — a doc-handle registry in the kit
- **Doc handle**: an opaque `{kind, id, block}` the editor addresses instead of a finding URL.
  - `finding/<uuid>/description`, `engagement/<uuid>/methodology`, `vector/<uuid>/<field>`, …
- **One endpoint pair** served by the kit (where the editor assets already live): `POST /_kit/autosave`
  (save) + `GET /_kit/autosave` (load), body/query carries the handle + (for save) the doc.
- **Registry**: `register_autosave_kind(kind, *, load, save, authorize)`.
  - `authorize(actor, id) -> bool` is **MANDATORY** — the registry refuses a kind without it, and the
    endpoint refuses an unknown kind (fail-closed). This is the one hardened path; see Authz below.
  - `load(id, block) -> doc`, `save(id, block, doc) -> rendered_html` (the kind owns its storage +
    HTML cache; the seam owns the route, the dispatch, and the authz gate).
- **Editor JS**: generalize `findingId` → `docHandle` (kind/id/block). `save`/`load` POST/GET the kit
  endpoint with the handle. `findingId` stays as a back-compat shim that synthesizes
  `finding/<id>/<block>` until all callers pass a handle.
- **Artifacts + presence** (image paste → `apiBase + /artifacts`; yjs presence) stay per-surface for now —
  they are orthogonal to the content write and already work; only the CONTENT autosave unifies in phase 1.

## Authz — the make-or-break
A generic "write field Y of entity X" endpoint concentrates tenancy risk: one dispatch bug is a hole
across EVERY surface. Non-negotiable:
- `authorize` mandatory per kind; unknown kind → 404/403; missing authorize at register time → raise.
- The resolver gets the HOST actor. Must handle BOTH a cookie session user and a PAT actor (the machine
  twin shares this path), resolving to the SAME tenancy predicate the surface already uses
  (`can_view_engagement` / vector's visibility stmt) — NOT a second copy (one home).
- **Invariant test** (`INV-TENANCY-*`): for every registered kind, (a) an authorized actor saves, (b) an
  unauthorized / foreign-tenant actor is refused, (c) an unregistered kind is refused, (d) a kind cannot
  be registered without an authorize fn. A new kind that forgets authz fails this test, not prod.

## Migration order (EDD — parity at each step, baseline the autosave round-trip first)
1. **Kit seam** — registry + endpoint + editor `docHandle` (back-compat `findingId` shim) + the authz
   invariant. No behavior change yet. **← phase 1, this branch.**
2. **scribble findings → kind** — `autosave_api` registers `finding`, its routes delegate to (or are
   replaced by) the seam. Parity eval: round-trip every DEFAULT_BLOCK, assert stored doc + cached HTML
   byte-identical to pre-migration.
3. **scribble library templates → kind** (`library_ui`).
4. **vector diagrams → kind** — `/vector/api` content write removed; vector registers `vector`.
5. **machine twin (`api_pat`) → the seam's save/authorize core** (one write path, two actor types).
6. **NEW: scribble engagement prose → kind** — methodology / scope / exec-summary as ProseMirror docs on
   the seam. THIS is what unblocks the original "editable prose + house editor" ask with zero new backend.

## Core mount (Eli: core-mount, 2026-10-14) + the `/_kit/` security trap

**`/_kit/` is a PUBLIC path.** `src/app/routes/auth.py::_is_public_path` returns True for `/_kit/`
("pure static JS/CSS, no secrets, same class as /static/"). Mounting the autosave endpoint at
`/_kit/autosave` naively would make it an **unauthenticated write endpoint** — a hole. The core mount MUST:
- **Carve `/_kit/autosave` OUT of the public path** so it falls through to the cookie auth gate:
  `if path.startswith("/_kit/") and not path.startswith("/_kit/autosave"): return True`. Assets stay
  public; the autosave write is authed.
- Stay **CSRF-protected** — `/_kit/` is NOT in `_CSRF_EXEMPT_PREFIXES`, so autosave keeps CSRF; the kit
  editor already sends the token on its autosave POST (same path class as `/scribble/api` today).
- Mount right after `ensure_kit_assets(app)` in `src/app/__init__.py`:
  `ensure_autosave_registered(app, actor=lambda: getattr(g, "current_user", None))`.
- **actor resolver** = the host's current dashboard user (`g.current_user`, per `extensions.py current_actor`).
  A kind's `authorize` may use it, or resolve its own (scribble's `finding` kind reuses its EXISTING
  `can_view_engagement` via `scribble.deps.current_actor()` — one home — rather than trusting the arg).
  Write-ROLE (viewer vs operator) is already enforced app-wide by core's gate; the kind's `authorize` owes
  only TENANCY (can this actor see the finding's engagement).

**Cross-repo ordering (corrected):** core's mount `import lotek_kit.autosave` fails until the kit ships that
module, so the order is: (1) ONE ext PR = kit `autosave.py` + scribble `finding` kind + editor `docHandle`
→ ext tag; (2) ONE core PR = re-pin + the `_is_public_path` carve-out + the mount + tests → release. Do NOT
commit the core mount before the re-pin (core won't boot).

## Notes / gotchas
- The editor is in the kit; the seam belongs there too so scribble AND vector mount one blueprint
  (`lotek_kit.flask_assets` is the precedent for a kit-served blueprint).
- Storage stays per-kind (findings keep content_json; prose moves to content_json docs) — the seam does
  not own schema, only the route + dispatch + authz.
- Reproduction-block verbatim rule + the variable overlay live in the RENDER path
  (`templating.lint.resolve_finding`), already unified — the autosave seam only stores + caches raw HTML;
  it does not resolve variables (autosave cache is the UNRESOLVED editor render, same as today).
- Don't fast-path this: it touches authz. The invariant must genuinely pass; `--ack-invariants` is not
  optional here.
