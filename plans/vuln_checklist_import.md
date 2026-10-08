# Vuln library + checklist import into scribble — assessment & implementation plan

**Status:** PLAN FOR REVIEW. No code committed. Awaiting direction on the decision points (§7).
**Date:** 2026-10-08
**Worktree:** `lotek-extensions` branch `vuln-library-import`
**Source:** a finding-template + test-plan export (372 finding templates, 517 test-plan items), generic suite.

---

## 1. What the source export contains

- **Vuln library** = 372 finding templates. Each: `name, cwe, cvss, recommended_severity, service_line, category, details.body (writeup, with {placeholder} author fill-ins), guidance.body (author/QA notes), recommendations, definition, additional_resources`.
- **Test plans** = 517 items across 11 `assessment_type`s. Each: `title, description (## Applies To / ## Playbook: When to Test / Setup / Steps / Tips), tags, assessment_type`.

## 2. How scribble models the targets (so the import is exact)

- **Vuln library → `VulnerabilityTemplate`** (`scribble/models.py:254`, table `scribble_vuln_templates`). Shared, tenant-free. Fields used: `name, category(free-text), default_severity(Severity enum: info/low/medium/high/critical), cvss_score, cvss_vector, content_json {description,details,remediation,reproduction → prosemirror}, references(list), active, tags(M2M)`. **No CWE/CVE column on the template** (those live only on per-engagement `BoardFinding`). Import = bulk JSON seeder `seed/loader.py:import_vuln_templates` (idempotent **by name**) → parses into prosemirror blocks.
- **Checklists → `ChecklistTemplate` + `ChecklistTemplateItem`** (`models.py:992`/`1017`). EXISTS and is fully built. `ChecklistTemplate{slug,name,description,kind,category,builtin}`; items `{section,text,guidance,framework,control_ref,default_status,order_index}`. `kind` = `coverage|reminder|compliance` (`enums.py:51`). `category` = assessment-type hint. Import = `seed/loader.py:import_checklist_templates` reading `seed/checklists/*.json` (idempotent **by slug**, never-clobber). Runtime import endpoint also exists: `POST /api/checklists/templates` (markdown or JSON).
- **Severity** maps 1:1 (source `critical/high/medium/low/informational` → scribble `critical/high/medium/low/info`).
- **AssessmentType** registry (`models.py:217`): Internal/External/Web App/Device-Mobile seeded; `ChecklistTemplate.category` hints by it.

**Fit verdict:** both targets import cleanly via JSON seed files + existing loaders. One small new loader for the richer vuln shape (keeps references + cvss + cwe-as-tag) — see §4.

## 3. Vuln overlap — merge assessment

- **372 templates → ~263 canonical families** by `base — <tech>` / `base [<platform>]` suffix pattern. 31 families absorb 140 templates.
- Biggest: `Sensitive Information Disclosure` (29), `Vulnerable Software` (14), `Exposed Administration Interfaces` (11), `Exposed Services` (9), `Insecure Network Transmission` (7), `Missing Authentication` (6).
- **Data-quality fixes found:** `Exposed Administration Interface` (3) vs `Interfaces` (11) = same finding split by typo; singular/plural and em-dash/hyphen inconsistency throughout.

**Merge is NOT blind name-collapse** — worked example, the `Insecure SSL/TLS` family:
| source name | service_line | severity | reality |
|---|---|---|---|
| Insecure SSL/TLS Configuration | network | informational | **protocol** (SSL2/3, TLS1.0/1.1) |
| Insecure SSL/TLS Configuration [Heroku] | cloud | medium | same protocol finding, platform variant → **MERGE** |
| Insecure SSL/TLS **Certificate** Configuration | network | medium | **certificate** validity/trust → **KEEP SEPARATE** |

→ Merge rule: collapse only **true variants of the same root cause** (platform/tech flavor); keep genuinely distinct root causes separate even when names rhyme. Severity varies across variants → merged entry takes the **highest** as default + a note. This per-family judgment is the main review item.

**Proposed merged `VulnerabilityTemplate` shape:**
- `name` = canonical base (normalized: plural, em-dash style consistent).
- `content_json.description/details/remediation` = best merged prose; tech-specific variant text folded into a **"Variants / affected technologies"** sub-section with `{placeholder}` fill-ins.
- `default_severity` = max across merged variants.
- `cvss_score/cvss_vector` = from source where present (most are null).
- `references` = union of `additional_resources`.
- `tags` = **applicability** (`external, internal, webapp, cloud, …` from service_lines) + **CWE** (`cwe:79` etc., since no CWE column) + root category.

## 4. Vuln import mechanism (proposed)

Add a small dedicated loader rather than forcing source data through the FACTION HTML parser (source prose is already clean markdown-ish; FACTION parser expects `# Description`/`# Impact` HTML markers it doesn't have):
- New `seed/curated_vulnerabilities.json` — the ~263 merged templates in a direct shape: `{name, category, severity, cvss_score, cvss_vector, references[], tags[], blocks:{description,details,remediation,reproduction}}`.
- New `seed/loader.py:import_curated_templates(session, json_path)` — idempotent by name (same contract as `import_vuln_templates`), maps directly to `VulnerabilityTemplate`, creates/links `Tag`s for applicability + CWE. Wire into `seed_defaults`.
- Generator script `scripts/build_vuln_library.py` (repo-side, not shipped) transforms the raw export → `curated_vulnerabilities.json`, applying the merge families + normalization. Re-runnable so re-exports re-merge deterministically.

## 5. Test-plan → checklist mapping (your assessment types + buckets)

Seven checklist templates (`seed/checklists/*.json`, `kind: coverage`), items = test-plan steps (`text`=title, `guidance`=playbook, `section`=tag/phase):

| # | slug | category | fed from | ~items |
|---|---|---|---|---|
| 1 | `external-network` | external | external-penetration-test (26) | ~26 |
| 2 | `internal-network-unauth` | internal | internal-pentest items w/o cred requirement | ~53 |
| 3 | `internal-ceded-access` | internal | internal-pentest AD/credentialed + host-based-review (56) | ~65 |
| 4 | `webapp-unauth` | web-app | app-pentest WEB items (frontend/backend/API tags) w/o auth | ~35 |
| 5 | `webapp-auth` | web-app | app-pentest WEB items requiring auth | ~20 |
| 6 | `red-team-vapt` | red-team | adversarial superset (recon→foothold→privesc→lateral→persistence→exfil→objectives) assembled across external+internal+webapp + the 4 red-team service-line items; longer adversarial-pressure engagement | ~40 curated |
| 7 | `other` | other | everything that doesn't map cleanly → cloud(103), PSR(80), arch(52), mobile(37), genai(30), wireless(21), hybrid(9), thick-client "Locally-installed Software"(46) | grouped by section |

**Split caveats (why some land in `other` or need your call):**
- auth/unauth and ceded/unauth are **not clean fields** in the export — derived from tags + description keywords (`authenticated/credential/ceded/post-exploitation/valid account/domain user`). Rough signal: app-pentest 57 unauth / 29 auth; internal 53 unauth / 10 ceded; host-based 35/21.
- app-pentest ≠ pure webapp: **46 items tagged "Locally-installed Software"** are thick-client → routed to `other`, not webapp.
- Items whose auth-mode is ambiguous → placed under a `Needs triage` section in the most-likely checklist (not silently dropped), for your manual sort.
- `red-team-vapt` is **curated, not a raw dump** — it reuses items from 1–5 arranged by kill-chain phase, so it's an editorial pass needing your review of phase coverage.

## 6. Importability (how you get them in)

- **Ship as builtin seed** (recommended): drop `curated_vulnerabilities.json` + `seed/checklists/{external-network,internal-network-unauth,internal-ceded-access,webapp-unauth,webapp-auth,red-team-vapt,other}.json`; they seed at boot, idempotent, never-clobber your edits.
- **Ad-hoc re-import**: checklists via `POST /api/checklists/templates` (JSON/markdown); vulns one-at-a-time via `POST /machine/templates` (PAT). No runtime **bulk** vuln import exists — adding one is optional (a `POST /machine/templates/bulk` could wrap `import_curated_templates`), flag if you want it.
- Everything round-trips: `GET /api/checklists/templates/<id>/export?format=json|md`.

## 7. Decision points — need your call before I build

1. **Merge aggressiveness:** merge true variants only, keep distinct root causes separate (my recommendation, per the SSL/TLS example) — or flatter/looser?
2. **Variant text:** fold tech-specifics into a "Variants" sub-section with `{placeholders}` (recommended) vs keep each tech as its own template (less merge).
3. **CWE storage:** as `cwe:NNN` **tags** (recommended, no column) vs drop vs stuff into category.
4. **auth/ceded split:** trust my tag+keyword heuristic and dump ambiguous into a `Needs triage` section, OR build each type as ONE checklist with `Unauthenticated`/`Ceded`/`Authenticated` **sections** instead of separate checklists?
5. **`other` bucket:** one big `other` checklist with per-domain sections, or separate per-domain checklists (cloud/mobile/wireless/…) parked under category `other`?
6. **Scope now vs later:** build all 7 now, or just your 6 active types (defer `other` domains)?
7. **Bulk vuln import endpoint:** add `POST /machine/templates/bulk`, or seed-file only?

## 7b. Decisions locked (2026-10-08) + what was built

Decisions: (1) true-variants-only merge; (2) fold variants into a sub-section, convert Quill `{name}`→
scribble jinja — but author fill-ins stay single-brace (jinja-inert) and only client tokens become
`{{COMPANY_NAME}}`, because scribble jinja-renders content and `{{a | b}}` would break; (3) CWE as
`cwe:NNN` tags; (4) separate checklists per auth-mode (your ask); (5) separate checklists for the
`other`/domain buckets; (6) build now; (7) add `POST /machine/templates/bulk`. Plus a **cloud** bucket;
the remaining domains (mobile/wireless/genai/PSR/arch/hybrid/thick-client) **deferred**.

Built (branch `vuln-library-import`, UNCOMMITTED — awaiting your review):
- `scripts/build_vuln_library.py` — deterministic generator (372→263 merge, brace/token normalization,
  consultant-note stripping across all delimiter styles). Verified 0 stray `{{ }}`, 0 note leaks.
- `scribble/scribble/seed/curated_vulnerabilities.json` (263) + `seed/checklists/*.json` × 7
  (external-network, internal-network-unauth, internal-ceded-access, webapp-unauth, webapp-auth,
  cloud-assessment, red-team-vapt).
- `seed/loader.py`: `import_curated_templates` + shared `build_template_from_record` + tag get-or-create,
  wired into `seed_defaults` (templates 63→323; 3 idempotent name-skips).
- `api_pat.py`: `POST /machine/templates/bulk` (write scope, idempotent by name, machine_authored),
  `api_schemas.BulkCreateTemplatesRequest`, `openapi.py` `_RESPONSES` entry, audit verb registered.
- `tests/test_curated_library.py` (seed, whole-library jinja-safety, 7 checklists, bulk endpoint) +
  updated 4 existing count/slug pins.

## 8. Implementation phases (after approval)

- **P1 — generator + merged vuln JSON.** `scripts/build_vuln_library.py` → `curated_vulnerabilities.json` (merge families applied). Human-diffable; you review the 31 merge families.
- **P2 — vuln loader.** `import_curated_templates` + wire into `seed_defaults` + tags. Test: seed idempotent, references/cwe-tags land.
- **P3 — 7 checklist JSONs.** Generated from test-plan items per §5; `Needs triage` sections flagged.
- **P4 — red-team-vapt curation.** Editorial kill-chain assembly; your review.
- **P5 — tests + docs.** Seed-idempotence tests, count assertions, scribble docs. No auto-commit; you approve each PR.
