# CREAM (quoting & invoicing)

CREAM is a lotek extension for client **quotes (SOWs) and invoices**. It mounts at **`/cream`** (browser
UI at `/cream/…`, cookie-authed JSON at `/cream/api`, PAT/Bearer machine API at `/cream/machine`) and
owns the `cream_*` tables in lotek's database. There is **no payment processing** — CREAM produces the
paperwork (a numbered, frozen HTML/PDF document with a scope appendix and an authorization-to-test
block); money moves somewhere else entirely. A document bills exactly one **engagement**, and that
engagement is the tenancy key the host uses to decide who may read or write it.

---

## Mount

Declared in `lotek-extension.toml`; lotek discovers the package through the `lotek.extensions`
entry point and reads the manifest from inside the installed wheel.

| Manifest key | Value |
|---|---|
| `[mount] name` / `entrypoint` | `cream` / `cream` |
| `[mount] url_prefix` | `/cream` |
| `[mount] seed` | `cream.seed:seed_defaults` (idempotent, re-run every boot) |
| `[host] machine_prefix` | `/machine` → `/cream/machine` |
| `[db] base` | `cream.models:Base` |
| `[db] table_prefix` | `cream_` |

`register()` signature (what the host mount framework calls):

```python
cream.register(app, engine, *, url_prefix="/cream", instance_path=None,
               base_template="cream/base.html", client_model=None,
               session_factory=None, create_tables=True, **host_models) -> CreamConfig
```

Three blueprints are registered: the UI (`/cream`), the cookie-authed JSON API (`/cream/api`), and the
PAT machine API (`/cream/machine`).

**Seed.** First boot creates the issuer singleton (so the branding page has a row) and a rate card named
`Default` with seven starter entries: `run_type:external_pentest` ($5000/project),
`run_type:internal_pentest` ($6000/project), `run_type:web_app` ($4500/project),
`run_type:ad_assessment` ($5500/project), `phase:retest` ($250/hr), `phase:reporting` ($225/hr),
`host-band:1-256` ($35/host). Edit them in the database or via the rate-card rows; re-running the seed
never overwrites an existing entry.

---

## UI surfaces

Two nav entries are declared in the manifest:

| Nav label | Path | Endpoint |
|---|---|---|
| CREAM | `/cream/` | `cream.dashboard` |
| CREAM branding | `/cream/brand` | `cream.brand_settings` |

Everything else is reached from those two.

### Documents list — `/cream/`

Every document the current actor may see, newest first, with kind, status, number, title, bill-to name
and total. Rows are filtered by the host's visible-engagement set: a document belonging to an engagement
you hold no membership on simply is not in the list. Links out to **New document**, and per row to Edit
(drafts) / View / PDF.

The **number** cell is also the row's link, so it never renders as a placeholder. A document with no
number yet (nothing is numbered before issue) shows a **handle** built from the tail of its id —
`draft …b839c91e20` — with the full id in the link's `title`; the same handle heads the document's own
view page. It used to be a bare `—`, which made a draft's whole click target one em-dash with no identity
in it (ext#46, client-reported). The leading word is the document's *status*, not the literal "draft",
because voiding a draft leaves it unnumbered but no longer a draft.

**The tail, not the head.** These ids are UUIDv7 — the first 48 bits are a millisecond timestamp, so
consecutively created ids share their front (lotek#336 measured five sharing their first 23 characters).
A head-truncated "short id" shows different documents as the same string and never errors. If lotek lands
#336's core-owned UUID-reference widget, this call site should move onto it rather than keep its own copy;
`cream/handles.py` deliberately follows that convention (tail, leading `…`, full id on hover).

A blank **bill-to** still renders `—` on purpose: that is a missing *field*, not a missing identifier, and
printing an id tail under a column headed "Bill to" would invent an identity for a client record that may
not exist yet.

### New document — `/cream/documents/new`

Four fields: kind (Quote/SOW or Invoice), **engagement id (UUID, required)**, title, bill-to name,
currency. It POSTs to the browser API and drops you in the editor. A quote is created with the
authorization-to-test block enabled; an invoice is not — both are changeable afterwards.

### Editor — `/cream/documents/<uuid>/edit`

Form on the left, **server-rendered live preview on the right**. The preview is produced by the real
renderer through a rolled-back savepoint, so what you see is what the PDF will be. Sections:

| Section | Contents |
|---|---|
| Document | Title, currency, client reference / PO |
| Bill to | Name, Attn, address, email — a **snapshot**, not a join to the host Client |
| Dates | Execution window start/end, quote valid-until, payment due |
| Line items | Add line, reorder, and **Suggest from rate card** |
| Adjustments | Discount label + percent **or** fixed amount, tax label + percent |
| Scope & authorization | Authorization-required toggle, signatory name/title, rules of engagement, **Pull scope from engagement** |
| Notes | Free text, rendered through CREAM's restricted markup |
| Quoted vs executed | Advisory burn comparison (never printed on a client document) |

Actions: **Save**, **Issue & freeze**. Opening the editor on a non-draft redirects to the frozen view
rather than showing dead inputs.

### Viewer — `/cream/documents/<uuid>`

The rendered document plus the lifecycle buttons: **Issue & freeze**, **Mark sent**, **Client accepted**
(quotes), **Convert to invoice** (accepted/issued quotes), **Void**, and the **HTML** / **PDF** exports.

### Branding — `/cream/brand`

The issuer identity every document is rendered with: company name, address, email, phone, website, tax
ID, logo (data URI), accent colour, font stack, default currency / tax label / tax rate, payment
instructions, footer terms, default RoE terms. **Writing branding is admin-only** — see Security.

---

## Data model

Four owned tables plus two singleton/bookkeeping tables, all `cream_`-prefixed. Every surrogate PK is a
**UUIDv7** (`uuid.uuid7`, monotonic — `ORDER BY id` is creation order).

| Table | Purpose |
|---|---|
| `cream_brand` | Issuer identity + house style. Singleton (`slot = "default"`). |
| `cream_rate_cards` | A named, active/inactive set of default prices. |
| `cream_rate_items` | `unit_key` → label + unit price + default unit. The suggestion source. |
| `cream_documents` | A quote or an invoice. |
| `cream_line_items` | One priced line on a document (`qty × unit_price`, ordered). |
| `cream_number_counters` | Locked per-`(kind, year)` issue sequence. |

### References into lotek core

`engagement_id`, `client_id`, `owner_id` and `document_object_id` are **UUID soft references** — the
value is a core row's id, but there is **no cross-schema foreign key** (an extension must not own an FK
into core, and CREAM also runs standalone). They are `Uuid`-typed, never `Integer`.

| Column | Points at | Nullable | Notes |
|---|---|---|---|
| `cream_documents.engagement_id` | core `Engagement` | **NOT NULL** | The tenancy key. Not settable through any edit path. |
| `cream_documents.client_id` | core `Client` | yes | Attribution only; bill-to is snapshotted separately. |
| `cream_documents.owner_id` | core `User` | yes | Who created it. Attribution, never authorization. |
| `cream_documents.document_object_id` | host object store | yes | Declared; **nothing writes it today** — see Gotchas. |
| `cream_documents.converted_from_id` | `cream_documents.id` | yes | Audit link from an invoice back to its quote. |

**No authorization data lives in CREAM.** Tenancy is the host's: `engagement_id` merely records which
engagement a document bills, and the host seam decides who may read or write it.

### Money

Amounts are `Numeric(12, 2)` mapped to `Decimal`, never `float`. Coercion goes through `str`, quantizes
half-up at every boundary, and `float()` appears only at the JSON edge. Percentages are clamped to
0–100; a discount is clamped to the subtotal (no negative totals). Totals order is owned in one place:
subtotal → discount → taxable → tax → total. The frozen snapshot stores money as **strings**, so a
reissued render cannot drift by a cent.

### Lifecycle

```
draft ──issue──> issued ──mark-sent──> sent
                    │                    │
                    └─────accept─────────┘ (quotes only) ──> accepted
                                                                │
   any state ──void──> void            convert (issued/sent/accepted quote) ──> NEW invoice draft
```

- **draft** — fully editable, no number.
- **issue** — assigns `INV-YYYY-NNNN` / `Q-YYYY-NNNN` from a locked counter row, stamps `issued_at`, and
  writes `snapshot_json` (lines, totals, bill-to, **and the issuer block**). An issued document
  **renders from that snapshot**, so changing your letterhead or a rate card next quarter cannot rewrite
  a document already in a client's hands. Any mutation attempt afterwards raises `DocumentFrozen` → 409.
- **mark sent** — only from issued/accepted.
- **accept** — quotes only, from issued/sent. This is what unlocks conversion.
- **convert** — copies an issued/sent/accepted quote into a **new invoice draft** (`converted_from_id`
  points back). The quote is never mutated in place.
- **void** — issued documents are voided, never deleted; the record survives.

---

## Machine (PAT) API

Bearer/PAT-authenticated, CSRF-exempt, session-free. Mounted at `/cream/machine`, authenticated by the
host's PAT authenticator as a blueprint `before_request`, and **scope-gated per route**. Identity comes
from the PAT principal; the same engagement gates apply as in the UI.

| Method | Path | Scope | Purpose |
|---|---|---|---|
| `GET` | `/cream/machine/documents` | `read` | List documents in the token's visible engagements. |
| `GET` | `/cream/machine/documents/<uuid:doc_id>` | `read` | Fetch one document (full JSON incl. line items + totals). |
| `POST` | `/cream/machine/documents` | `write` | Create a **draft** quote/invoice against an engagement. |
| `POST` | `/cream/machine/documents/<uuid:doc_id>/line-items` | `write` | Append a line item to a draft. |
| `POST` | `/cream/machine/documents/<uuid:doc_id>/sync` | `write` | Return **suggested** line items from the rate card. Writes nothing. |

That is the whole surface. **There is deliberately no `/issue`, `/void`, `/mark-sent`, `/accept`,
`/convert`, `/scope-sync`, `/preview`, `/burn` or `/brand` on the machine API** — a PAT drafts, a human
finalizes. A test in this package fails the build if a finalization verb ever appears here.

Request bodies are declared as pydantic models (`cream/api_schemas.py`), so they are hoisted into the
host's OpenAPI `components.schemas` as real types rather than prose.

### Discovery

You do not need this table to drive CREAM. lotek's API is self-describing, and **an extension's machine
routes are included automatically** — the host's generator picks up any route whose view carries the
conventional scope attribute stamped by `require_scope`:

- `GET /api/v1/openapi.json` — the full spec, including `/cream/machine/*` with their scopes and bodies.
- `GET /api/v1/guide` — the narrative agent guide.

See lotek's own API documentation for token issuance, scopes and error conventions; none of that is
restated here.

### Worked example

```sh
# create a draft invoice against an engagement the token's user operates on
curl -sS -X POST https://lotek.example/cream/machine/documents \
  -H "Authorization: Bearer $LOTEK_PAT" -H 'Content-Type: application/json' \
  -d '{"kind":"invoice","title":"External network penetration test",
       "engagement_id":"0198e2c1-6a5c-7c31-8f0e-1c2d3e4f5a6b"}'

# add a line
curl -sS -X POST https://lotek.example/cream/machine/documents/$DOC/line-items \
  -H "Authorization: Bearer $LOTEK_PAT" -H 'Content-Type: application/json' \
  -d '{"description":"External network penetration test","qty":1,"unit":"project",
       "unit_price":5000,"source":"run_type:external_pentest"}'

# ask what the rate card would suggest for units not yet on the document
curl -sS -X POST https://lotek.example/cream/machine/documents/$DOC/sync \
  -H "Authorization: Bearer $LOTEK_PAT" -H 'Content-Type: application/json' \
  -d '{"unit_keys":["run_type:external_pentest","phase:reporting"]}'
```

Then open the document in the dashboard and press **Issue & freeze**.

### Machine-surface response codes

| Code | Meaning |
|---|---|
| `400` | Missing/non-UUID `engagement_id` or `client_id`. |
| `401`/`403` | Host PAT auth or scope refusal; `403 forbidden` also means "not an operator on this engagement". |
| `404` | Document missing **or** outside the token's visible engagements (deliberately indistinguishable). |
| `409` | `conflict` — the document is issued/void and can no longer be modified. |
| `503` | No host injected the PAT hooks (standalone / misconfigured mount). Fails closed. |

---

## Browser JSON API — `/cream/api`

Distinct surface, **cookie + CSRF**, driven by CREAM's own editor. It is a strict superset of the
machine API and is where the finalization verbs live: `PUT /documents/<id>` (whole-draft save),
`POST …/preview`, `POST …/scope-sync`, `GET …/burn`, `POST …/issue`, `…/mark-sent`, `…/accept`,
`…/convert`, `…/void`, plus `GET/PUT /brand` and `GET /health`. It takes a session cookie, so it is not
usable by an agent holding only a PAT — that separation is the point.

---

## The deliverable

CREAM has its **own** renderer (`cream/render.py`) — it does not depend on any other extension's report
engine. One `DocumentView` goes through one function for all three outputs, so the preview cannot show
something the export will not.

| Output | Route | Notes |
|---|---|---|
| Preview fragment | editor pane (`POST …/preview`) | Rendered from unsaved state inside a rolled-back savepoint. |
| Standalone HTML | `GET /cream/documents/<uuid>/export.html` | Full page, inline `<style>`, `Content-Disposition: attachment`, filename = document number. |
| PDF | `GET /cream/documents/<uuid>/export.pdf` | Requires the optional `weasyprint` extra. |

The rendered document contains: issuer block (logo, address, contact, tax ID) · document heading, number
and status pill · issued / valid-until / due / execution-window / client-reference metadata · bill-to
block · line-item table with per-line detail prose · totals (subtotal, discount, tax, grand total) ·
**Appendix A — Scope of testing** when the document carries a scope list · **Authorization to test**
with signature lines when `authorization_required` · notes, payment instructions and footer terms.

**PDF is optional.** Without `weasyprint`, `export.pdf` returns **HTTP 503** with a plain-text
explanation — never a 500, and never HTML wearing a `.pdf` filename. Install it with:

```sh
pip install 'cream[pdf]'
```

---

## Host seam

CREAM reads optional host capabilities from `config.extras` at request time. Every one is fail-safe, and
the authorization ones fail **closed**.

| Hook | Used for | Absent → |
|---|---|---|
| `pat_authenticate` / `require_pat_scope` / `pat_actor` | Machine API auth, scope, principal | 503 (machine API unusable) |
| `current_actor` | Attribution + admin check | No attribution; standalone treated as admin |
| `can_write` | UI nudge only (the host role gate is the real control) | `True` |
| `can_operate_on(engagement_id)` | Write authorization, checked **before** every write | `True` standalone; error → `False` |
| `visible_engagement_ids()` | Read scoping of every list/get | `None` (no scoping); error → empty set |
| `engagement_scope(engagement_id)` | "Pull scope from engagement" → Appendix A | `[]` |
| `engagement_units(engagement_id)` | Browser "Suggest from rate card" | `[]` |
| `engagement_burn(engagement_id)` | Quoted-vs-executed panel | `{}` |

---

## Security posture

- **Financial finalization is human-only.** Issue, void, mark-sent, accept and convert exist only on the
  cookie-authed browser surface. A PAT can draft and price; it cannot freeze a numbered document or
  cancel one. This is enforced by route topology (the verbs are not registered on the machine
  blueprint), not by a runtime flag, and is guarded by a test.
- **Engagement authorization happens before the write, on the document's engagement.** Mutating routes
  resolve the document first and then ask the host about *that document's* `engagement_id` — never about
  an engagement id supplied in the request body.
- **Not-visible reads 404, not 403**, on both the UI and both APIs. A 403 would confirm that a document
  with that id exists, which is precisely what an id-guessing probe wants.
- **Branding writes are admin-only.** `PUT /cream/api/brand` carries `payment_instructions` — the block
  telling a client where to send money. Gating it on ordinary write capability would let anyone who can
  edit a line item silently re-route remittance on every future document, with no per-document trace.
- **The logo must be an inline raster `data:` URI** (`png|jpeg|jpg|gif|webp`), ≤ ~2 MB, no quotes/angle
  brackets/whitespace. Validated when stored **and again at render time** with the identical allowlist.
  A remote URL is dropped, never fetched: the PDF engine fetches what a document references, so an
  `http(s)` logo would turn "render this invoice" into a server-side request to an attacker-chosen host.
  SVG is excluded on purpose. `weasyprint` is also invoked without a `base_url`, so no relative
  reference can resolve to anything.
- **Accent colour and font stack are pattern-matched**, not escaped-and-passed-through — they land
  inside a `<style>` block, where an unvalidated value could close the element.
- **Free text is escape-first.** Line-item detail, notes, addresses and terms go through CREAM's tiny
  markup subset: `html.escape` the whole string, *then* apply a handful of regexes to the already-escaped
  text (paragraphs, `- ` bullets, `**bold**`, `*italic*`, `` `code` ``). No author-typed tag can survive,
  because by the time formatting runs there are no `<` characters left. There is no raw-HTML path.
- **The editable field set is a whitelist.** `update_document` cannot reach `status`, `number`,
  `engagement_id` or `snapshot_json` — the fields that would let a caller forge a document's identity or
  its tenancy.
- **Issue numbering is race-safe**: a locked `(kind, year)` counter row, not a `MAX()` scan.

---

## Gotchas

- **Three host hooks are not wired in lotek today.** `engagement_scope`, `engagement_units` and
  `engagement_burn` are not among the extras lotek injects. Consequence on a stock install: *Pull scope
  from engagement* freezes an **empty** Appendix A, the browser *Suggest from rate card* button returns
  **no** suggestions, and the quoted-vs-executed panel reports unavailable. The machine `sync` route
  still works because the agent supplies `unit_keys` in the body — it never consults the host seam.
- **Machine `create` applies fewer fields than the browser `create`.** The browser route also runs the
  full editable-field update (bill-to, dates, notes, discount…). The machine route sets only kind,
  title, engagement, client, currency, tax label/rate, RoE terms and the authorization flag — and there
  is no `PUT` on the machine surface. An agent-drafted document gets its bill-to and dates from a human
  in the editor.
- **`document_object_id` is declared but never written.** Issued documents are not currently pushed into
  the host object store; the exports are downloads. Do not build a workflow that assumes a stored
  artifact exists.
- **No `client_ref_table` in the manifest**, so lotek's generic client-delete guard does not count CREAM
  rows. Deleting a core Client can leave `cream_documents.client_id` dangling. Documents still render
  correctly — bill-to is a snapshot, not a join — but the attribution link is lost.
- **`engagement_id` is required and immutable.** There is no way to create an unattached document and no
  way to move one to another engagement. Wrong engagement → void it and start again.
- **Standalone mode is not a security mode.** With no host hooks, `can_operate_on` is `True`, there is no
  engagement scoping, and the actor is treated as admin. That is correct for a single local user and
  wrong for anything shared.
- **Every column added after the first release must be nullable or defaulted.** CREAM's `create_all`
  runs an additive `ADD COLUMN` migration and *skips* a column that is neither — it would silently never
  appear on an already-deployed instance.
- **A draft is not a record.** Only issuing produces a number and a snapshot. A draft is editable by any
  operator on its engagement and carries no guarantee that what you saw is what somebody else sees.
- **Re-issuing raises.** Converting an accepted quote produces a *new* invoice draft; it does not reopen
  the quote.
- **`sync` requires an editable document.** Called against an issued or void document it returns 409,
  even though it writes nothing.

---

Source: <https://github.com/ShyftXero/lotek-extensions> (`cream/`).
