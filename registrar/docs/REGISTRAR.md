# Registrar — offensive-infrastructure inventory + control

Registrar is a bundled lotek extension for **tracking and controlling the infrastructure you stand up
during an engagement** — servers (C2, redirectors), domains, and DNS. It mounts at **`/registrar`** and
is **provider-agnostic**: a backend (DigitalOcean/libcloud, a DNS registrar, an SMS gateway) is a
*driver* selected per action, so the same verbs work whichever provider owns the resource. The bundled
build ships an in-memory **`null`** driver (no egress, no credentials) so the whole staging/audit gate is
exercisable before you wire real provider keys. Outward actions are **confirm-tier**: an agent on a token
can *stage* infra work but can never *fire* it — a human approves in the dashboard.

Registrar is consumed by lotek as a **pinned git dependency** (a `lotek.extensions` entry point), not
vendored and not staged by any script. See `README.md` for the dependency/mount mechanics; this document
is the operator reference for the running surface.

---

## Where it mounts

| Thing | Value |
|---|---|
| URL prefix | `/registrar` |
| Entry point | `lotek.extensions` → `registrar` (module exposing `register(...)`) |
| Browser UI | `/registrar/` |
| Cookie-authed JSON API | `/registrar/api` |
| PAT/Bearer machine API | `/registrar/machine` |
| Owned tables | `registrar_*` |
| Seed | none (Registrar has no seed step) |

The host injects its engine, session factory, and PAT/capability hooks at mount time; standalone
Registrar degrades every host hook to a safe local default. The machine API **fails closed (503)** if no
host provides PAT authentication.

---

## UI surface

Registrar declares a single nav entry (`⌘ Registrar`) pointing at one page:

**`/registrar/` — Infrastructure dashboard.** Three read-only tables:

- **Servers** — name, kind (`static`/`transient`), state (`planned`/`active`/`missing`/`destroyed`),
  provider, IP, role. Scoped by the read rule below.
- **Domains** — name, provider, registered (yes/no).
- **Recent actions** — the last 10 audit rows (time, verb, tier, result).

The dashboard is inventory + audit only. There is no create/destroy button on the page — outward changes
go through the API's tiered `action` flow (below), and a `can_write` host signal drives a UI nudge, not
the enforcement.

---

## Data model

All tables are `registrar_`-prefixed and live in the shared host database. Every surrogate PK is a
**UUIDv7** (application-generated, monotonic — `ORDER BY id` sorts by creation). `engagement_id` /
`checked_out_to` are **UUID soft references** to a core lotek `Engagement`: **no cross-schema FK**
(Registrar can run standalone), just the UUID recorded on the row. Registrar stores **no authorization
data and no `client_id`** — tenancy is the host's, resolved per request through the host seam.

| Table | Holds | Key columns / relationships |
|---|---|---|
| `registrar_servers` | Tracked hosts | `kind` (static/transient), `state`, `name`, `provider`, `provider_ref`, `ip`, `role`, `engagement_id` (soft ref; transient hosts are engagement-bound, static float to null), `destroyed_at` |
| `registrar_domains` | Domain inventory | `name` (unique), `provider`, `registered`, `checked_out_to` (engagement soft ref; null = available) |
| `registrar_dns_records` | DNS records under a domain | `domain_id` → `registrar_domains` (FK, `ON DELETE CASCADE`), `rtype`, `name`, `value`, `ttl` |
| `registrar_staged_actions` | Confirm-tier actions awaiting human approval | `verb`, `provider`, `args_json` (real args, needed to execute — never in the audit), `engagement_id`, `initiator_id`, `status` (pending/executed/rejected), `confirmed_by`, `confirmed_at` |
| `registrar_audit` | Append-only local trail | `at`, `actor`, `verb`, `provider`, `tier`, `detail` (secret-free projection), `result` (executed/staged/error) — **insert-only**, never updated or deleted |

**Attribution is denormalised on purpose** (provider/ref/ip/timestamps live on the row) so a record
**outlives the provider it came from** and stays meaningful after the resource is gone.

### Read rule

- **Transient** servers are scoped to the engagements the caller may read (via the host
  `visible_engagement_ids` seam; for a PAT that resolves from the token principal).
- **Static (org) infra is admin-only.**
- Standalone (no host actor) is treated as a single local admin — nothing is scoped out.

---

## Action-safety model

Every driver verb declares an **effect-locality tier** — decided by *where the effect is observable*, not
by reversibility:

- **`direct`** — a read / an internal change. Runs **inline**.
- **`confirm`** — any verb whose effect is observable **outside this instance** (a provider mutation, a
  DNS record at a resolver, an SMS to a real phone). **Never runs autonomously.** It is **staged** and
  executes only from an explicit human approval step.

The host **resolves a verb's tier and fails closed**: an unrecognized verb is treated as `confirm` and
staged, never executed.

| Verb | Axis | Tier | Behavior via `POST .../action` |
|---|---|---|---|
| `list_nodes` | compute | direct | runs inline (200) |
| `get_node` | compute | direct | *declared direct in the driver contract but not currently wired into the action dispatcher — returns `bad_request`* |
| `create_node` | compute | confirm | staged only (202) |
| `destroy_node` | compute | confirm | staged only (202) |
| `list_records` | dns | direct | runs inline (200) |
| `upsert_record` | dns | confirm | staged only — a record write takes effect at third-party resolvers and is TTL-cached, so its effect is outward |
| `register_domain` | dns | confirm | staged only (202) |
| `send_sms` | sms | confirm | staged only (202); recipient + body are **redacted** from the audit |
| *(any unknown verb)* | — | confirm | staged only — fail closed |

### The two-person approval gate

A staged (`confirm`-tier) action executes through **one path only** — the cookie-authed
`POST /registrar/api/staged/<id>/approve`. Server-side, that path requires **all** of:

1. an **interactive dashboard session** (a human; any PAT/machine caller is rejected),
2. a **confirmer different from the initiator**,
3. **live write authorization**, and
4. for an engagement-bound action, the confirmer's **own operator capability on that engagement**,
   re-checked at execution time (never inherited from the initiator).

There is deliberately **no approve route on the machine API** — see below.

---

## Machine (PAT) API

Mounted on its own blueprint at **`/registrar/machine`**. This is the surface host tools/agents drive with
a **Bearer personal access token + scope RBAC** — the same contract as lotek's own `/api/v1` and the
other extensions' machine surfaces. It is distinct from the cookie-authed browser API at `/registrar/api`;
the host exempts this prefix from the session gate and CSRF because it takes no cookie. Each route
authenticates in `before_request` and is gated by a required scope; identity comes from the PAT principal.

| Method | Path | Scope | Purpose |
|---|---|---|---|
| GET | `/registrar/machine/servers` | `read` | List servers visible to the token's principal (transient engagement-scoped; admin sees all). |
| GET | `/registrar/machine/domains` | `read` | List all domains (name, provider, registered, checkout). |
| POST | `/registrar/machine/action` | `write` | Run a **direct**-tier verb inline (200); **stage** a confirm-tier verb (202). Never executes a confirm-tier verb. |
| GET | `/registrar/machine/staged` | `read` | List pending staged (confirm-tier) actions awaiting a human approval. |
| GET | `/registrar/machine/audit` | `read` | The 50 most recent audit records (verb, provider, tier, result, redacted detail). |

`POST /registrar/machine/action` request body (`ActionRequest`):

```json
{ "verb": "list_nodes", "provider": "null", "args": {} }
```

- Direct-tier verb → `200 {"status":"executed","detail":{...}}`.
- Confirm-tier (or unknown) verb → `202 {"status":"staged","staged_id":"<uuid>","note":"..."}`.
- Missing `verb` → `400`; a direct call that resolves confirm-tier → `409`.

**There is no `POST /registrar/machine/staged/<id>/approve` route, by design.** A PAT is never
interactive, so it can never satisfy the approval gate — omitting the route makes "*a PAT stages, a human
approves*" explicit at the surface instead of only enforced a layer down. An agent's reach is:
read inventory → stage an outward action → read the pending queue and the audit. Firing it stays human.

### Discovery

An agent does not need this document to find the surface. lotek's OpenAPI generator treats any extension
view carrying the machine-scope marker as a PAT-drivable endpoint and hoists its request model into the
spec's components, so **Registrar's machine routes and the `ActionRequest` schema appear automatically**
in:

- `GET /api/v1/openapi.json` — the full machine surface, this extension's routes included.
- `GET /api/v1/guide` — the agent-oriented walkthrough.

Use those for the authoritative, live contract (paths, scopes, schemas); this table is the operator
summary, not a second source of truth for the core API.

---

## Output / deliverable

Registrar does not render a client report (that is Scribble's job). Its operator-facing output is:

- **The infrastructure inventory** — the `/registrar/` dashboard and the `servers` / `domains` list
  endpoints, kept accurate against providers by the reconcile pass (a transient server the provider no
  longer reports is flagged **`missing`**, loudly — never silently dropped or terminalized on an
  unreachable provider).
- **The audit trail** — the defensibility artifact. Every privileged action writes a **secret-free**
  row to `registrar_audit`, and when mounted the *same call, in the same transaction* also appends to
  core `audit_events` via the host `audit` seam (`ext:registrar:<result>`). That dual write is what lets
  the engagement's infra actions be reconstructed against lotek's central trail.

---

## Providers / drivers

Three pluggable axes, each an ABC: `ComputeDriver`, `DnsDriver`, `SmsDriver`. The MVP ships one in-memory
`null` backend per axis (no egress, no credentials) so the gate/audit/staging machinery runs end-to-end
before any provider is wired. Real backends implement the same ABCs and are opt-in:

```sh
pip install .[providers]   # apache-libcloud (compute + DNS) + twilio (SMS)
```

The provider is chosen per action (`"provider"` in the request body); `"null"` is the default no-op
backend. An unknown provider slug falls back to the `null` backend.

---

## Gotchas & security posture

- **Confirm-tier is human-only; a PAT can only stage.** No machine approval route exists. Approval
  requires an interactive session, a *different* confirmer, live write authz, and (for engagement-bound
  actions) a re-checked operator capability.
- **Fail closed on unknown verbs.** Anything not in the driver verb tables is confirm-tier → staged,
  never executed.
- **The machine API fails closed (503) with no host.** Standalone Registrar has no PAT scheme, so a
  machine route with no host gate refuses rather than running unauthenticated.
- **Audit is secret-free.** An SMS recipient/body and provider credentials **never** land in
  `registrar_audit` or `audit_events` — the detail is an allow-listed projection. The real args live only
  on the staged-action row, needed to execute.
- **Tenancy is the host's.** Registrar stores no authorization data. Transient servers are
  engagement-scoped through the host seam; static org infra is admin-only. A mounted host-hook failure
  fails **closed** (empty visibility, no operate capability); standalone degrades to a single local admin.
- **`registrar_audit` is insert-only** — no UPDATE, no DELETE path exists in code.
- **Reconcile flags drift, it does not reconcile destructively.** A transient server missing from the
  provider is marked `missing`; it is never auto-destroyed on an unreachable provider.

---

## Reference

- Repository: <https://github.com/ShyftXero/lotek-extensions>
