# Scribble's id identity — decision doc

**Standing design doc, not a branch plan.** Written 2026-08-15 after a PAT-driven client tripped over
scribble's ids twice in one session (a 404, then a 500). Records what is actually true, what it costs to
change, and — the part that reframes the question — a **core-side blocker that has nothing to do with
scribble**.

Status: **awaiting a decision.** Nothing here is committed to beyond the measurements.

---

## The headline

> Core's `jobs.promoted_ref_id` is `Integer`, and `host_contract.mark_job_promoted` does `int(ref_id)`.
> **No extension with UUIDv7 primary keys can record a promotion.**

And it fails in the worst available way. `int(uuid.UUID(...))` does **not** raise — `UUID.__int__` is
defined, so the coercion quietly succeeds and yields the 128-bit integer:

```python
>>> int(uuid.uuid4())
54119763906123297665350571515781126704      # 126 bits
```

The boundary check that should have caught a type error passes. The failure surfaces later, at the
write, as a *numeric value out of range* from the database (Postgres `int4` tops out at 2³¹−1; even
`bigint` would not hold 126 bits, and SQLite's 8-byte integer overflows too). So the error a developer
sees names arithmetic, not identity, and points at the database rather than at the seam that mangled the
value — verified 2026-08-15, not inferred.

`CLAUDE.md` opens the v2-native contract for every extension with "UUIDv7 surrogate PKs". The host seam
that records a promotion accepts an int only. **The documented contract is unimplementable, as written,
for any extension that promotes a job** — including a hypothetical new one built to spec on day one.

Scribble is not behind the standard here so much as it is the only extension that has ever exercised the
seam, and it exercises it with the int PKs that made the seam look correct.

## What is actually true (measured, prod + `origin/main` 2026-08-15)

| | |
|---|---|
| Core `engagements.id`, `findings.id`, `assets.id`, `jobs.id`, `clients.id` | `uuid` |
| `cream_documents.*`, `registrar_*.engagement_id`, `vector_diagrams.*` | `uuid` |
| **`scribble_engagements.id` and 17 other `scribble_*` PKs** | **`integer`** (sequential) |
| `scribble_enrichment_proposals.id` / `.engagement_id` | `uuid` — scribble's own odd one out |
| Scribble's soft host refs (`client_id`, `owner_id`, `source_finding_id`, `asset_id`) | TEXT `SoftHostId` |
| **Core `jobs.promoted_ref_id`** | **`integer`**, indexed `ix_jobs_promoted`, **write-only** — no reader in core app code, in any extension, or in a template |

Blast radius of a scribble PK migration: **20 tables (18 with an Integer PK), 22 intra-scribble FK
edges, 48 `<int:…>` route converters, 4 `_opt_int` call sites, 9 `parseInt`/`Number` id coercions in the
front end.** Prod data volume is negligible — 190 rows total, 2 engagements, 0 findings, and most of it
is seed data (63 vuln templates, 87 checklist items).

So the cost is **code and URL surface, not data migration**.

## The two problems this causes today

1. **A core engagement UUID is not a scribble engagement id, and nothing links them.** `scribble_engagements`
   has no column pointing at a core engagement — only `client_id` (a core client UUID) and `guid`. So
   `POST /scribble/machine/engagements/<core-uuid>/findings` returns **404** and the caller has no way to
   discover the mapping. A PAT client hit exactly this before it hit the 500 in
   `fix/scribble-softhostid-retrofit`.
   - `guid` (`String(64)`, unique, `models.py:94`) is **entirely unused** — no reader, no writer, no
     template. Either it is the intended carrier for this link or it is a dead unique column, which is a
     trap either way.
2. **Sequential ids are enumerable.** What prevents cross-tenant reads is the tenancy predicate
   (`can_view_engagement` / `visible_engagement_ids`), not the id being unguessable. That is a real
   defence and it is tested — but scribble is the only surface with no *second* layer under it. Treat
   this as defence-in-depth, not as an open IDOR.

## Options

### A. Leave it
Cost: nothing. Keeps all three problems, including the unimplementable contract for the next extension.

### B. Add a core-engagement link column
`scribble_engagements.core_engagement_id: SoftHostId` (or repurpose `guid`), populated on create and on
`promote-job`, plus a lookup so `GET /scribble/machine/engagements?core_engagement_id=<uuid>` resolves.
- Fixes problem 1 (discovery) — the one that actually blocks machine clients today.
- Fixes neither 2 nor the seam.
- Cost: one column, one query path, one test. Small.
- Decide `guid`'s fate in the same change rather than leaving a dead unique column beside a new one.

### C. Widen the host seam (core / lotek repo) — **recommended first, regardless of the rest**
`jobs.promoted_ref_id` `Integer` → text-backed soft ref; drop the `int(ref_id)` coercion in
`mark_job_promoted`; widen the `ref_id: int` annotation.
- Unblocks the documented UUIDv7 contract for **every** extension, present and future.
- Independent of whether scribble ever migrates — an int ref keeps working, stored as text.
- Cheap and low-risk *because the column is write-only*: no reader anywhere means no downstream parsing
  to chase. Alembic migration + `tests/test_job_promoted_ref.py` already exists as the guard to extend.
- The guard to add is specifically **a UUID `ref_id` round-tripping intact** — not "does it raise". Given
  `UUID.__int__`, a test that only asserts an error is thrown would pass today against the *wrong* error
  from the wrong layer.
- **Watch for the same trap this doc's sibling branch fixed:** an existing `jobs` table keeps its native
  INTEGER column, and Postgres will then refuse every write — core runs Alembic, so express it as a
  real migration, not a model-only edit.

### D. Migrate scribble's own PKs to UUIDv7
The full alignment. Requires **C first**, or promote breaks the moment the ids change.
- 18 PKs, 22 FK edges, 48 route converters, 9 front-end coercions, plus every bookmarked
  `/scribble/engagements/2` URL breaking.
- Data migration is trivial at current volume; the risk is entirely in coverage of the URL/JS surface.
- Buys: one id shape across the whole product, no enumeration, and problem 1 dissolves (a scribble
  engagement could simply *be* keyed by the core engagement UUID where one exists).

## Recommendation

**C → B → decide on D.**

C is small, is in the highest-leverage place, and is the only item that unblocks something *written down
as a contract and currently false*. B removes the wall machine clients actually hit. D is a real
improvement but it is defence-in-depth plus consistency, and it is the only option whose cost is measured
in days rather than hours — so it deserves an explicit yes, not a drift into it.

## Related

- `plans/fix-scribble-softhostid-retrofit.md` — the 500 that started this; same root family (a core id
  shape meeting a column or a parser that assumes `int`).
- `CLAUDE.md` §"v2-native contract" and §"the core-ref column trap".
