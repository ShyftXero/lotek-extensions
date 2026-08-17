# Scribble (reporting)

Scribble is a pentest **vulnerability-template library plus an engagement/report engine** — a Python
re-imagining of OWASP FACTION's write-up library and report generation. It is a lotek **extension**: a
separate installable package that lotek discovers through the `lotek.extensions` entry-point group and
mounts at **`/scribble`**, turning scan output and hand-authored findings into a client deliverable
(self-contained HTML, an HTML+artifacts zip, or an editable `.docx`). It loads only when the extension
is enabled; disabled, none of its routes or tables are live. This page covers Scribble only — how
extensions load, how PATs are minted, and how scans run belong to lotek's own manual.

---

## Mount points

| Prefix | Auth | What it is |
|---|---|---|
| `/scribble/…` | host session cookie | the human UI (pages, forms, report renders) |
| `/scribble/api/…` | host session cookie, **CSRF on** | the browser JSON API the pages drive |
| `/scribble/machine/…` | `Authorization: Bearer lotek_pat_…`, **CSRF exempt** | the PAT machine API (see below) |

The three prefixes are deliberately disjoint. `machine_prefix = "/machine"` is declared in
`lotek-extension.toml`'s `[host]` table; that declaration is what makes the host skip its cookie/session
gate and CSRF protection for exactly that sub-path — it must never be widened to cover `/scribble/api`.

## What seeds on first boot

The manifest declares `seed = "scribble.seed:seed_defaults"`; the host runs it once after mounting, and
it is idempotent (a second boot is a no-op, and an operator's in-place edit of a seeded row is never
clobbered).

| Seeded | Count | Notes |
|---|---|---|
| Vuln templates | **63** | 44 converted from FACTION's default library + 19 lotek AD/network/web entries |
| Assessment types | 4 | Internal, External, Web App, Device / Mobile — each with a colour and default board order |
| Built-in `{{VARIABLE}}` keys | 10 | `COMPANY_NAME` `ENGAGEMENT_NAME` `TARGET_HOST` `TARGET_PORT` `TARGET_URL` `ASSESSOR` `TODAY` `START_DATE` `END_DATE` `SEVERITY` |
| Fact-mapped report variables | 8 | `AFFECTED` `DOMAIN` `TARGET_URL` `TARGET_HOST` `MAX_SEVERITY` `AFFECTED_COUNT` `ACCOUNTS` `OBJECTS` |
| VulnMap rows | 11 | scan-finding signature → library template (kerberoast, asreproast, certipy, secretsdump, responder, enum4linux ×3, brutus, dalfox, kubescape) |
| Checklist templates | 7 | `global-pre-engagement` `network-infrastructure` `web-app-api` `owasp-wstg` `owasp-asvs-l1` `pci-dss-segmentation` `ai-llm-security` |

Template import is idempotent **by name**, VulnMap by its `(source, title_pattern, dedupe_prefix)`
match-key, checklists **by slug and never-clobber**. Report variables are the one upgrade-refreshing
seed: an existing key has only its `from_facts`/`target_column` overwritten so a shipped mapping fix
reaches an already-seeded database, while its label/scope/type stay as the operator left them.

---

## UI surfaces

Four sidebar entries come from the manifest's `[[nav]]` tables, in this order:

| Nav label | Path | What it does |
|---|---|---|
| Scribble | `/scribble/` | Dashboard: engagement/finding/client counts + the 10 most recent engagements, **scoped to the clients you can see**. The Vuln Templates tile is the one deliberately global number — the library is a shared, tenant-free table. |
| Report Boards | `/scribble/engagements` | Engagement list + **New** (name, scope type, company name, optional dates, select-or-create client). An engagement is the container for a report's findings, groups, checklists and artifacts. |
| Vuln Library | `/scribble/library` | The seeded templates plus anything you author. Search by name, filter by category/severity/tag, toggle inactive templates into view. |
| Assessment Types | `/scribble/assessment-types` | The user-managed lookup that names board groups (report sections). Not hardcoded — add your own. |

One further page exists without a nav entry: **`/scribble/checklists`**, the checklist library
(create / import / edit / hide / reset / duplicate / export). Reach it directly.

### The drag board

`/scribble/engagements/<id>` is a two-level drag-and-drop tree — **assessment-type groups → findings**.
Board order *is* document order.

- Create a group from a seeded (or custom) assessment type; add findings to it from a library template,
  or drag existing findings between groups.
- Each group orders its findings **by severity** (`auto_severity`, worst-first) or **manually**. Any
  drag into a group flips it to manual; `POST /scribble/api/groups/<id>` with
  `{"order_mode": "auto_severity"}` — the board's *re-rank by severity* control — is the way back.
- Ungrouped findings sit in their own bucket, always shown severity-first.
- A group can be excluded from the rendered report without deleting it. **Deleting a group detaches its
  findings** (they go ungrouped) rather than destroying them — a report section is not the same thing as
  the findings inside it. **Deleting a finding does take its artifacts with it**, rows and files both.

Drag-and-drop is native HTML5 with no external sortable library; lotek is CSP-strict, so nothing here
loads from a CDN.

### Finding editor + artifact gallery

`/scribble/findings/<id>` is where the finding is written up. Metadata (title, category, severity,
confidence, status, CVSS score/vector, target host/port/URL, include-in-report) sits above a per-block
rich-text editor. Each content block autosaves independently as you pause typing; the saved HTML is
re-rendered and cached (`content_html`) so list and preview surfaces never drift from the source JSON.

The editor is a dependency-free `contenteditable` surface that reads and writes the same
ProseMirror-JSON document contract TipTap produces — a vetted offline TipTap bundle is a documented
drop-in, but is not what ships.

The **artifact gallery** attaches evidence per finding — screenshots, text, arbitrary files — each with
a caption, an include/exclude toggle and a drag-to-reorder position. Artifacts are always served as
forced `attachment` downloads, never inline in the app origin: evidence can be attacker-influenced (a
scraped page, raw tool output) and must not render inside the dashboard's own origin.

### Live co-editing — what is and is not wired

Read this before promising it to a team:

- **Server side is complete.** A per-`(finding_id, block)` CRDT room (`pycrdt`, the Python bindings for
  Yjs's Rust core) is served over `flask-sock` at `/scribble/ws/findings/<id>/blocks/<block>`, speaking
  the real Yjs sync + awareness wire protocol. Room state persists to `scribble_collab_docs` and is
  reconciled back into the finding's `content_json`/`content_html` on room close.
  `GET /scribble/api/findings/<id>/blocks/<block>/collab-status` reports whether a room is live and how
  many clients are attached.
- **A real vendored Yjs browser client ships** in the package (`scribble/static/collab.js` plus
  `scribble/static/lib/`), but the finding editor page does **not** load it today — the shipped editor
  mounts autosave plus HTTP-polled presence
  (`POST|GET /scribble/api/findings/<id>/blocks/<block>/presence`) only. So out of the box, concurrent
  edits to the same block are last-writer-wins per block, not merged.
- Even with the client attached, merge granularity is **whole-document per debounce**, not per
  keystroke: character-level interleaving and remote cursors need the editor itself to be a real
  ProseMirror/TipTap instance.

Attribution (`created_by`, `owner_id`) is recorded but is **never an access gate** — engagements are
team-shared, and tenancy is decided by client membership (below).

---

## Data model

Scribble owns every table prefixed **`scribble_`** (`[db] table_prefix` in the manifest;
`scribble.models:Base` is the authoritative DDL). 21 tables:

| Area | Tables |
|---|---|
| Engagements | `scribble_engagements`, `scribble_finding_groups`, `scribble_findings`, `scribble_artifacts` |
| Library | `scribble_vuln_templates`, `scribble_vuln_map`, `scribble_assessment_types` |
| Variables | `scribble_variables`, `scribble_variable_values` |
| Tags | `scribble_tags`, `scribble_finding_tags`, `scribble_template_tags` |
| Reports | `scribble_report_templates`, `scribble_report_renders` |
| Checklists | `scribble_checklist_templates`, `scribble_checklist_template_items`, `scribble_engagement_checklists`, `scribble_engagement_checklist_items` |
| Collab | `scribble_collab_docs` |
| Enrichment | `scribble_enrichment_proposals` |
| Standalone-only | `scribble_clients` (empty when mounted — the host's client table is used instead) |

### How it references the host

Every link to lotek core is a **soft reference**, never a foreign key — that is what lets the same
package run standalone or mounted, unchanged.

| Column | Points at | Shape | Notes |
|---|---|---|---|
| `scribble_engagements.client_id` | lotek `Client` | int **or** UUID | The tenancy anchor. Resolved through the host-injected client model at read time, not a relationship. |
| `scribble_engagements.owner_id` | lotek `User` | int **or** UUID | Attribution only. Never an access gate. |
| `scribble_findings.source_finding_id` | lotek `Finding.id` | int | Set when a scan finding is promoted; the key promotion dedupes on. |
| `scribble_findings.asset_id` | lotek `Asset` | int | Optional. |
| `scribble_enrichment_proposals.finding_id` / `.engagement_id` / `.decided_by` | lotek core rows | UUID | This table is v2-native: UUIDv7 PK, `Uuid` columns throughout, `engagement_id` NOT NULL for tenancy. |

`client_id`/`owner_id` use a custom `SoftHostId` column type precisely because a lotek v2 host has
UUIDv7 primary keys while a standalone/legacy one has sequential ints — both round-trip.

Job→engagement linkage is **not** a Scribble column on lotek's `Job`. Promotion records it on lotek's
own generic `Job.promoted_extension` / `Job.promoted_ref_id` through the host seam, the same two columns
any extension uses; core never interprets what `ref_id` means to the extension that wrote it.

Scribble also never imports lotek's `Finding`/`Job` ORM classes. It reads
`get_job` / `list_findings` / `get_finding` off the injected host services namespace, which return plain
DTOs. Severity validates against the host's own enum when injected; the two vocabularies are
value-for-value identical (`info` `low` `medium` `high` `critical`), so mounting never changes which
severity strings are accepted.

---

## Machine (PAT) API

Nine routes under `/scribble/machine`, Bearer-token authenticated, scope-gated, CSRF-exempt. They mint
no auth of their own: the blueprint's `before_request` delegates to the host's PAT authenticator, and
each route's scope check delegates to the host's own RBAC. `write` scope already implies the token's
user is operator/admin, not a demoted viewer.

| Method · path | Scope | Purpose |
|---|---|---|
| `POST /scribble/machine/engagements` | write | Create an engagement. `name` **and** `client_id` required when mounted; optional `scope_type` (default `external`), `company_name`. |
| `GET /scribble/machine/templates` | read | List active library templates. Filters: `?q=` (name contains), `?category=`, `?severity=`. |
| `GET /scribble/machine/templates/<template_id>` | read | One template with full `content_json`, CVSS and references. 404 if missing or retired. |
| `POST /scribble/machine/engagements/<engagement_id>/findings` | write | Add a finding from a `template_id` **or** by promoting one `lotek_finding_id`. Optional `group_id`, `target_host`, `target_port`, `target_url`. |
| `POST /scribble/machine/engagements/<engagement_id>/artifacts` | write | Upload evidence — `multipart/form-data` (`file` field) or JSON with `content_base64` (aliases `data_base64`, `data`) + `filename`. Optional `finding_id`, `caption`, `kind`, `placement`, `idempotency_key`. |
| `POST /scribble/machine/engagements/<engagement_id>/promote-job/<job_id>` | write | **Bulk-promote every finding of a scan job** into the engagement. |
| `POST /scribble/machine/vuln-map` | write | Curate a scan-finding → template mapping. `template_id` required, plus at least one of `source`, `title_pattern`, `dedupe_prefix`. |
| `GET /scribble/machine/vuln-map` | read | List the mappings. |
| `POST /scribble/machine/resolve-template` | read | Resolve `(source, title, dedupe_key)` to a mapped `template_id`, or null. |

**Discovery.** An agent does not need this table. lotek's `GET /api/v1/openapi.json` is introspective and
every enabled extension's machine routes appear in it automatically — including their scopes and, where
declared, typed request bodies (`CreateEngagementRequest`, `AddFindingRequest`,
`UploadArtifactRequest` are hoisted into `components.schemas`). `GET /api/v1/guide` is the prose
companion. Read those for the auth scheme, token format and error envelope; this page does not restate
them.

### Refusal codes worth knowing

| Situation | Response |
|---|---|
| Extension disabled / not mounted | Flask's plain **404** — the blueprint does not exist. There is no "extension not enabled" 503 to special-case. |
| Mounted without a host bundle (standalone Scribble) | **503** `unavailable` — the machine API refuses rather than running unauthenticated. |
| Engagement missing, **or** its client outside your grants | **404**, byte-identical either way. Never 403 — a distinguishable refusal is an existence oracle over the whole id space. |
| `client_id` outside your grants on create | **404**, same reasoning — and byte-identical for "no such client" and "exists but you hold no grant". The `detail` carries a STATIC next-step hint: a core client created with `POST /api/v1/clients` is *record-only*, and the first engagement under it (`POST /api/v1/engagements`, **admin-only**, which self-grants the creator an `operator` membership) is what mints the membership Scribble checks. Appended unconditionally, so it distinguishes nothing between the two cases. |
| `client_id` omitted while mounted | **400**. A client-less engagement is readable by nobody, creator included, so creating one is a 201 for work that produced nothing usable. |
| Job missing, or one you cannot view | **404**, decided inside the host, never by Scribble. |
| Artifact over 25 MiB | **413**. |

### Getting scan findings in

```
1. Run a scan in lotek                 job completes, findings normalized
        |
2. POST /scribble/machine/engagements  { "name": "Q3 external", "client_id": <the job's client_id> }
        |
3. POST /scribble/machine/engagements/<id>/promote-job/<job_id>
        |                              -> every scan finding lands in the engagement,
        |                                 template-rendered or bridged verbatim
        |                              -> { engagement_id, promoted, skipped, parents }
4. Arrange the board                   groups, ordering, exclude what doesn't belong
        |
5. Edit each finding                   write-ups, severity/CVSS/target, evidence artifacts
        |
6. Export                              HTML (print to PDF) / zip / .docx
```

The easiest source of a correct `client_id` is the job you are about to promote — `GET /api/v1/jobs/<id>`
returns it, and an engagement collecting a job's findings belongs to that job's client by construction.

Promotion behaviour worth relying on:

- **Explicit and operator-triggered.** There is no automatic post-scan hook and no one-click
  *promote to Scribble* button on a job page. Nothing floods an engagement, nothing touches the scan
  pipeline. A job page only surfaces the assigned engagement's name *after* a promotion has happened.
- **Idempotent.** Every promoted finding stamps `source_finding_id`, so a re-run skips exactly what it
  already promoted regardless of title collisions across hosts, and re-POSTing a single
  `lotek_finding_id` returns the existing finding with `"deduped": true` instead of a duplicate.
- **Template-aware.** Each finding resolves through the VulnMap to a library template (rendered from
  that write-up) or, failing a match, is bridged verbatim from the scan finding's own prose.
- **Aggregating.** Findings that resolve to the same template are nested one level: a parent finding
  carries the write-up, per-host children carry their own target and evidence. The report renders that
  nesting. (Nesting is produced by promotion; there is no drag-to-nest control on the board.)

### VulnMap resolution order

`scribble_vuln_map` is a small operator-curated table with a real foreign key into the template library.
Resolution is **most-specific-first**:

1. `dedupe_prefix` — the finding's `dedupe_key` starts with the prefix; longest prefix wins, ties break
   to the lowest id.
2. `source` + `title_pattern` — case-insensitive glob against the finding's title (`*sql injection*`
   for a substring match; a plain string matches exactly).
3. `source` alone.

A stale mapping (template deleted or retired) resolves to null, so the caller cleanly falls back to the
verbatim bridge instead of erroring.

### Facts → report variables

Beyond the 10 structural built-ins, a promoted finding's `{{VARIABLE}}` placeholders are filled from the
scan tool's own structured evidence — the neutral facts a tool declares it emits, mapped to Scribble's
variable vocabulary **declaratively**, by rows in `scribble_variables` (`from_facts` + an optional
`target_column`). No tool name and no `if source == …` branch exists anywhere in the mapping, on either
side of the host boundary. `target_column` is allowlisted to `target_host`, `target_port`, `target_url`
and re-checked at the write site, so a hand-edited row can never steer a write at an arbitrary column.

When several child findings aggregate under one parent, group-level variables are synthesized
deterministically — `AFFECTED` is the sorted distinct hosts, `ACCOUNTS`/`OBJECTS` the union across
children, `AFFECTED_COUNT` and `MAX_SEVERITY` derived the obvious way. Table-driven pure functions, no
model in the loop.

---

## Deliverables

Three outputs come off one engagement context (`ReportContext` — the single structure both renderers
consume, so HTML and `.docx` cannot drift apart):

| Route | Output |
|---|---|
| `GET /scribble/engagements/<id>/report` | Live self-contained HTML, assets embedded. |
| `GET /scribble/engagements/<id>/report/export?format=html` | The same page as a downloaded `<name>-report.html`, artifacts inlined. |
| `GET /scribble/engagements/<id>/report/export?format=zip` | `<name>-report.zip` — `report.html` beside an `artifacts/` folder it links into. |
| `GET /scribble/engagements/<id>/report.docx` | `<name>-report.docx`, rendered via `docxtpl` from Scribble's own authored `default.docx`, evidence images embedded inline. Editable in Word. |

What enters the report: groups in board order (a synthetic *Ungrouped* bucket last), findings ordered by
the group's own mode, `include_in_report` respected at group, finding **and** artifact level, plus a
severity rollup, a generated executive-summary narrative paragraph, and any assigned checklists that opt
into the report.

There is **no server-side PDF renderer**. The HTML is a print-to-PDF deliverable; the `.docx` is the
editable hand-off.

Both renderers read artifact bytes through a reader confined to Scribble's artifact directory
(`<instance>/artifacts/`), so a crafted `storage_path` cannot escape it, and the `.docx` renderer skips
any single artifact over 25 MiB rather than pulling it into memory.

---

## Checklists

Non-blocking coverage/reminder/compliance lists. Nothing here gates an operation or withholds a report.

- **Library** at `/scribble/checklists`: create, import, edit in place, hide, reset to the shipped
  default, duplicate, export. Editing a builtin flips `customized` (which drives a *modified from
  default* hint plus Reset); hiding drops it from the picker without deleting it.
- **Assignment** is 0..N per engagement and is a **snapshot** — items are copied on assign, so a later
  library edit never rewrites a delivered engagement. `template_id` on the assignment is provenance
  only, not a foreign key, so deleting a library template never touches an assigned copy.
- Item `status` is free text (the UI offers the kind's recommended values but accepts a custom label);
  the rollup buckets it. A failed coverage item can link the finding that documents it.
- Three kinds — `coverage`, `reminder`, `compliance` — and the set is fixed, because the report layout
  and status vocabulary are keyed to it. Compliance items carry free-text `framework` + `control_ref`
  for the attestation appendix.

---

## Security posture and gotchas

**Tenancy is asked of the host, never decided here.** Scribble holds no access policy of its own. Every
engagement-scoped surface resolves to the engagement's `client_id` and asks the host's `can_view_client`
seam. Three shapes, all live:

1. A blueprint-wide fail-closed `before_request` gate on both cookie blueprints, driven by the route's
   own URL converters (`engagement_id`/`eid` directly; `finding_id`/`group_id`/`artifact_id`/`cid`/`iid`
   resolved to their engagement). A recognized id that does not resolve aborts 404 too.
2. Explicit per-route checks on the machine blueprint, which cannot use that gate — its principal is the
   PAT actor, not a browser session, so the shared gate would 404 every machine route.
3. Filtering (not aborting) on list surfaces — the dashboard and engagement list scope in SQL when the
   host supplies a visible-client-id set, otherwise through the per-client predicate. An empty set means
   *this actor holds nothing*, which is not the same as *no set available*; conflating them is how a
   fail-closed check turns fail-open.

Fail-closed specifics: a mounted host that exposes no `can_view_client` gets a refusal, not a fallback
to a local rule. Standalone Scribble (no host bundle) has no authorization model at all and enforces
nothing — that is the one permissive path, and it cannot occur inside lotek.

**A route that moves data between two tenancy domains checks both ends.** `promote-job` spans the
source job (host-decided) and the destination engagement (`can_view_engagement`). Until 2026-08-12 it
checked only the source, which meant a `write` token could pour its own scan results into any client's
report. If you add a route that reads from one tenant and writes to another, check both ends or it is
the same bug.

**Ownership is not authorization.** `Engagement.owner_id` is attribution. Do not reintroduce it, or the
host's `Job.owner_id`, as an access key.

**Evidence is untrusted input.** Artifacts always download as attachments, never render inline in the
app origin. Uploads are capped at 25 MiB (checked after base64 decode, with a preflight cap on the
encoded string so an oversized payload is refused before it is buffered), filenames are sanitized and
stored under a UUID-prefixed name inside a per-engagement directory.

**Machine-surface hygiene.** The CSRF exemption on `/scribble/machine` is only sound because those
routes accept no ambient session cookie. Never add a cookie fallback there, and never widen
`machine_prefix` to cover `/scribble/api`.

**Schema is additive.** `create_all` adds tables and can retrofit a plain new column or index onto an
existing table, but it will not rebuild one — so a database created by an old checkout keeps that
checkout's constraints. Every read site treats a retrofitted column as possibly NULL
(`finding.variables or {}`), and artifact idempotency is enforced by a lookup on
`(engagement_id, idempotency_key)` rather than a composite UNIQUE constraint that could not be added
retroactively.

**Enrichment proposes, never applies.** The severity-enrichment seam writes a *proposal* row for a
human to accept or reject and never mutates core scan data. The shipped driver is the null one: no
lookup, no egress, no proposals. A real driver replaces it.

---

Source, issues and the rest of the extension set:
[https://github.com/ShyftXero/lotek-extensions](https://github.com/ShyftXero/lotek-extensions)
