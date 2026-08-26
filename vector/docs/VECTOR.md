# Vector (attack paths)

Vector is a mountable lotek extension for authoring interactive attack-path / kill-chain diagrams
**entirely in the browser** and exporting them as a single **self-contained interactive HTML**
deliverable — the viewer runtime (JS + CSS) and the model JSON inlined into one file that renders
offline with **no external requests**. Mounted in a host it lives under **`/vector`**, owns its own
`vector_`-prefixed tables in the shared database, and never imports host internals — everything
host-specific (engine, session factory, base template, current-actor / can-write / PAT hooks) arrives
by injection through `register()`. It also runs standalone (`python -m vector`) against its own SQLite
DB for offline authoring.

Vector is consumed by lotek as a **pinned git dependency** discovered through the `lotek.extensions`
entry point — not vendored, not staged by any script. Enable it in the host (Settings → Extensions →
`vector` → reload); on first boot its seed inserts one read-only example.

---

## UI surfaces

One sidebar entry (`[[nav]]` label **Vector**, icon `◭`, path `/`). All paths below are relative to the
`/vector` mount prefix.

| Path | Purpose |
|---|---|
| `/` and `/diagrams` | **Diagram list** — the same view under both routes. Table of the diagrams you can see (name, phase count, node count, updated). Per-row **Open / HTML / JSON**, plus **Duplicate** and **Delete** for writers. Toolbar: **New diagram**, **Import JSON**. |
| `/new` | **New diagram** — opens the editor on a blank model. Write-gated: redirects to the list if the host says you can't write. |
| `/edit/<id>` | **Editor** — the authoring surface (below). Loads a diagram you're allowed to see. |
| `/diagrams/<id>/export.html` | GET download of the self-contained HTML deliverable. |
| `/diagrams/<id>/export.json` | GET download of the normalized `vector.attackpath/v1` model. |
| `/settings` | **My settings** — your own Vector preferences, reached from the ⚙ cog in the appbar. Requires a host identity; standalone Vector has one user and no page. |

### The editor

A split screen driven by the **real viewer runtime** (`static/vector-viewer.{js,css}`) — the same code
the deliverable ships, so the live preview can never drift from the exported file.

- **Left** — tabbed property panels: **Meta · Zones · Nodes · Edges · Phases · Style**. You edit an
  in-memory model; every change re-renders the preview.
- **Right** — the live preview plus a **phase scrubber** (range slider) to step the walkthrough while
  you author. (Keyboard capture is off in the editor preview; it is on only in an exported deliverable.)
- **Toolbar** — **Save** (writers only), **Export JSON**, **Export HTML**.

### Auto mode (the animated walkthrough)

The viewer plays the phases as a timed animation, advancing **one phase every 2600 ms**. Nodes pop in
at the phase their state timeline reaches, `beacon` nodes emit a pulsing ring, the current phase's edge
draws itself on, and `flow`-kind edges show a marching dash. In an **exported deliverable** the keyboard
is captured: **Space** toggles play/pause, **←/→** step, **Home** resets to phase 0. `prefers-reduced-
motion: reduce` disables the transition animations (the graph still advances).

### Browser JSON API (`/vector/api`, cookie-authed)

The editor and list drive a cookie-authenticated, CSRF-protected JSON surface: `GET /health`,
`GET|POST /diagrams`, `GET|PUT|DELETE /diagrams/<id>`, `POST /import`, `POST /diagrams/<id>/duplicate`,
`POST /export.html` (export unsaved editor state). This is the **human/browser** surface — distinct from
the machine (PAT) API below, which accepts no cookie.

---

## Data model

### The document — `vector.attackpath/v1`

A diagram is one JSON document. `vector/schema.py::normalize` coerces, caps, and drops malformed input
and **never raises** (the same document is embedded verbatim into an exported HTML file). Parts:

- **meta** — `title`, `subtitle`, `badge`, `railLabels[]`, and an `intro` block (`eyebrow`, `objective`,
  `readingNotes`, `note`).
- **zones** — trust-zone columns laid out left→right by `order`. Each has a title/subtitle and an accent
  (`red`/`orange`/`cyan`/`amber`/`green`/`violet`/`slate`).
- **boundaries** — optional firewall/segmentation markers drawn between zones.
- **nodes** — hosts/assets, each in a `zone` + `row`, carrying a **state timeline** (`states[]`:
  `{at: <phase>, state, label}`). States `target` < `owned` = `beacon` < `impacted` by precedence — the
  highest-precedence reached state shows; `beacon` emits a pulsing ring. Optional `role`, `dualIp`, and a
  `reIp` event (a phase at which the host changes IP/domain).
- **edges** — attacker actions between two nodes; `kind` picks the style (`attack`, `transfer`, `c2`,
  `tunnel`, `ssh`, `disc`, `mesh`, `action`, `disrupt`), pinned to a phase via `at`. An edge whose
  endpoints aren't real nodes is dropped.
- **phases** — the ordered walkthrough. **Phase 0 is the intro.** Each later phase carries a `title`,
  MITRE `tactics` chips, a red-team narrative (`desc`), the `targets` it lights up, and an optional
  blue-team `blue` block (tool, finding, query, what was seen, note, and a `gap` flag).
- **style** — optional catalog overrides; the JS runtime owns the vocabulary and merges `style` over its
  baked-in cyber-dark defaults, so a minimal diagram renders with no style block.

Bounds are enforced (e.g. ≤40 zones, ≤600 nodes, ≤1500 edges, ≤200 phases) so an exported deliverable
stays finite.

### Owned tables (`vector_` prefix)

Declared in `[db]` (`base = vector.models:Base`, `table_prefix = vector_`) so a host can carry them
through migrations and attribute them for cleanup:

| Table | Notes |
|---|---|
| `vector_diagrams` | One row per diagram: `name` + the whole model as JSON text (`model_json`), plus `builtin`, `created_by`, and the soft references below. **PK is UUIDv7.** |
| `vector_clients` | Minimal standalone Client (id, name). Stays empty when a host injects its own `client_model`. |

**References to the host are soft** (no foreign key), because Vector may run standalone where those host
tables don't exist. `owner_id`, `client_id`, and `source_job_id` on `vector_diagrams` are **UUID
references** to lotek core's `User` / `Client` / `Job` (all UUIDv7 in lotek v2). Ownership is an access
scope + soft attribution: a diagram is visible to its `owner_id`, to admins, and — for `builtin`
examples — to everyone; a NULL owner is admin-visible only.

### Seed

`vector.seed:seed_defaults` runs once after `register()` (idempotent). It inserts one read-only `builtin`
example — **"Spark Range — Red Team Attack Path (example)"**, a 16-phase IT→OT red-team kill chain (plus
phase 0 intro). It is visible to everyone and read-only: duplicate it to author your own, you cannot edit
or delete it.

---

## Machine (PAT) API

Mounted on its own blueprint at **`/vector/machine`** (`vector/api_pat.py`). This is the surface a host
**tool / agent** drives with a personal access token — `Authorization: Bearer lotek_pat_…` + scope RBAC —
the same contract lotek's `/api/v1` and scribble's machine API use. It is **disjoint from** the
cookie-authed browser API at `/vector/api`, accepts **no cookie**, and the host exempts this prefix from
its session gate and CSRF.

| Method | Path | Scope | Purpose |
|---|---|---|---|
| `GET` | `/vector/machine/diagrams` | `read` | List diagrams visible to the token's user (own + builtin; admin sees all). |
| `GET` | `/vector/machine/diagrams/{diagram_id}` | `read` | Fetch one diagram's normalized `vector.attackpath/v1` model. |
| `POST` | `/vector/machine/diagrams` | `write` | Create a diagram owned by the token's user. Body: `{name?, model?}`. |
| `PUT` | `/vector/machine/diagrams/{diagram_id}` | `write` | Update name and/or model. Owner or admin only; builtin is read-only. |
| `DELETE` | `/vector/machine/diagrams/{diagram_id}` | `write` | Delete a diagram. Owner or admin only; builtin cannot be deleted. |
| `GET` | `/vector/machine/diagrams/{diagram_id}/export.html` | `read` | Render a saved diagram to the self-contained HTML deliverable (report evidence to attach). |

Request bodies (`CreateDiagramRequest`, `UpdateDiagramRequest`) are declared as pydantic models, so the
host's OpenAPI generator hoists them into `components.schemas` with real types.

### Discovery

You don't hand-maintain this table for an agent. Every enabled extension's machine surface appears
automatically in the host's introspective spec: point an assistant at **`GET /api/v1/guide`** (it lists
each mounted extension with its `machine_prefix` and `pat_drivable` flag) and **`GET /api/v1/openapi.json`**
for the full typed spec. Vector's routes show up there because `@host.require_scope(...)` stamps the
conventional scope attribute the generator reads, and the pydantic bodies feed its schema hoist. See
lotek core's API docs for the token/scope model — this doc doesn't restate it.

---

## Output / deliverable

Both exports come from the model **as normalized on the server**, so the file can never carry a shape the
renderer would choke on.

- **Export HTML** — one self-contained `.html` that auto-boots the viewer from an inlined model. Runtime
  (JS + CSS) and model JSON are embedded → works offline, **no external requests**. The model is embedded
  inside a `<script type="application/json">` block with `</script>` / HTML-comment / JS line-separator
  sequences neutralized, so author text can't break out of the file. This is the deliverable an agent
  attaches to a report (via the machine `export.html` route) or an author downloads from the editor / list.
- **Export JSON** — round-trips the model as a `vector.attackpath/v1` document, re-importable via the
  editor's **Import JSON** or `POST /vector/api/import`.

The exported HTML carries the host-held **`deliverable_footer`** admin setting (below) as a fixed
footer line, when one is set. It is autoescaped — an admin typing HTML into the settings form must not
inject markup into a file a client opens.

---

## Settings — two scopes, two homes

Vector has both kinds of knob, and they deliberately live in different places (lotek#485 /
lotek-extensions#111). The rule: **a knob that changes the install for everyone is admin-scope and
belongs to the host; a personal preference belongs to Vector.**

### Admin / per-install — declared to the host

| Key | Type | What it does |
|---|---|---|
| `deliverable_footer` | `str` | Optional line stamped into the footer of every exported attack-path HTML — e.g. *"CONFIDENTIAL — prepared by Contoso Security"*. Blank = no footer. |

Declared as `[[settings]]` in `lotek-extension.toml`. The **host** renders the form (⚙ cog on Vector's
row at *Settings → Extensions*), enforces the **admin-only** gate, stores the value, and writes an
audit row for every change. Vector only reads it, through `deps.host_setting("deliverable_footer", "")`
(which resolves the injected `extras["extension_setting"]` late, per request, and degrades to the
default standalone or on any error). See lotek's `docs/EXTENSIONS.md` → *Settings*.

### User / per-user — Vector's own

| Key | Type | What it does |
|---|---|---|
| `hide_builtin_diagrams` | `bool` | Keeps the seeded read-only example out of **your** diagram list. Off by default. |

Stored in Vector's own `vector_user_prefs` table (unique soft `owner_id` → the host user), edited at
`/vector/settings` behind the ⚙ cog in Vector's appbar. No admin gate and no audit row, because
changing it changes nothing for anyone else. The form carries **no owner field** — the row is keyed off
`current_actor_id()` alone, so there is nothing for a caller to point at someone else's preferences.

🔴 **A preference is never an authorization input.** `blueprint.visible_diagrams_stmt()` is the IDOR
guard and is deliberately blind to `hide_builtin_diagrams`; the list view filters what that guard
already returned. Folding a preference into an access predicate is how a preference bug becomes a
disclosure bug.

---

## Security posture / gotchas

- **Fail closed when unmounted.** The machine blueprint's `before_request` delegates to the host's PAT
  authenticator; with no host injected (standalone, or a host that provided no PAT hooks) every machine
  route returns **503**, never an open door. Standalone Vector has no PAT scheme at all.
- **Every machine route is scope-gated.** `@host.require_scope("read"|"write")` per route — a read-only
  token is refused by **every** write verb (POST/PUT/DELETE → 403), not just create. A write token can't
  out-rank a demoted owner: the host's scope gate re-checks the owning user is still write-capable.
- **Tenancy is against the token's principal, not a session.** A PAT request carries no session, so the
  machine API resolves the actor from the PAT principal (`host.actor()`) and applies own + builtin (admins
  all) against **that** identity. A diagram owned by token principal 7 is not handed to a browser user who
  happens to be a different id.
- **No existence oracle.** A missing diagram and one you're not allowed to see both return **404** (not
  403) on the machine and browser surfaces alike — an id-guessing probe learns nothing.
- **Builtin examples are read-only to everyone**, including a write token and an admin: PUT/DELETE on a
  `builtin` row → 403. This is Vector's human-only-equivalent guard (there is no separate "confirm-tier"
  verb on this extension; the whole CRUD + export is PAT-drivable, but the seeded example is immutable).
- **The machine prefix must stay disjoint from the browser `/api`.** CSRF-exemption + session-gate skip
  are sound only because `/vector/machine` accepts no cookie; never add a cookie fallback there and never
  widen the prefix to overlap `/vector/api`.
- **Defense in depth in-app.** When mounted, the host's role gate is the real write authority and mutating
  browser routes stay CSRF-protected; Vector *additionally* checks `host_can_write` and per-row ownership
  itself (and for the standalone case, which has no host gate).
- **Key-type lag in the machine module.** `vector.models.Diagram` and its `owner_id` are UUIDv7, but
  `api_pat.py` still declares its `{diagram_id}` route as an integer and its owner logic assumes an integer
  principal id — a documented lag pending a machine-API key port (see the module docstring and the
  `plans/` follow-up). A principal id the module can't store degrades **loudly** to a NULL owner
  (admin-visible only) rather than binding a mismatched key; it never silently claims success.
- **Host integration — seeding from a scan lives in lotek core**, not in this extension. lotek's job page
  offers "Send to Vector", which builds a conforming scaffold (zones from subnets, nodes from assets,
  phases from top findings, **edges intentionally empty**) and drops you into the editor. The assembler
  and that route are core concerns — see the lotek core manual; Vector only owns the authoring and the
  model contract.
