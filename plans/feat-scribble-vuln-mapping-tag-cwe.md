# Plan: feat/scribble-vuln-mapping-tag-cwe

- **Branch:** `feat/scribble-vuln-mapping-tag-cwe`  (off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress (core logic + tests done; target-A deferred; suite+PR pending)

## Purpose
Scan findings promoted into a scribble engagement mostly land **flat/ungrouped** because the
`ScribbleVulnMap` only had 11 rows (AD/SMB/dalfox/kubescape). Nuclei — the dominant finding producer —
had **zero** map coverage, so every nuclei hit fell through to a verbatim per-host entry (a UKG report
had 118 flat nuclei rows: `weak-cipher` ×18, `expired-ssl` ×13, …). The library already has 323 vuln
templates; the gap was the mapping.

The durable fix is **not** per-nuclei-id rows (thousands, brittle). Nuclei's own **tags** are a precise
classification (`sqli`→SQL Injection, `lfi`→Path Traversal, …) and the finding DTO already carries
`facts["tags"]` + `facts["cwe"]` — the resolver just never looked. So: match on **tag/cwe**, which lets
~15 rows cover ~5000 nuclei templates and makes new templates inherit a mapping for free.

## Done
- [x] `ScribbleVulnMap`: `tag` + `cwe_id` columns + migration `c8e1a4f7b230` (off head `b2c4e6f8a1d3`).
- [x] `resolve_vuln_template`: `tags`/`cwe`; precedence `dedupe_prefix > title > tag > cwe > source`.
- [x] `_matched_template` + `api_pat.scribble_resolve_template` thread `facts['tags']`/`facts['cwe']`.
- [x] `seed_vuln_map`: `tag`/`cwe` keys + skip bare `_comment` strings.
- [x] `lotek_vuln_map.json`: tag taxonomy + whole-toolchain source rows + TLS/cert/metrics refinements.
      (Dropped `nuclei:cve-` catch-all: dedupe_prefix outranks tag, it would clobber accurate tags.)
- [x] SSTI template authored + `ssti` tag mapped to it.
- [x] **Re-map a job** (`remap_job_findings` + `/remap-job/<job_id>` route + Source-jobs button).
- [x] Adopt-picker finding/severity badge (extension render side).
- [x] Finding-page preview `parseInt(uuid)` regression fixed.
- [x] Tests: precedence, shipped sqli/TLS, end-to-end tag grouping, remap+idempotence, badge, preview
      guard, remap-button render.

## Remaining
- [ ] **target-A** (parent target-field/prose consistency on a multi-host parent) — deferred: touches
      CLIENT report prose (`report_variables.json` shape + applying overrides to the parent), wants its
      own eval'd commit. Form columns are already blank-by-design today; the open bit is `{{TARGET_HOST}}`
      prose still rendering the full host list.
- [ ] Full extension suite + `--ack-*`; open PR.

## Notes / gotchas
- **Cross-repo:** the core half of the badge (adoptable_jobs returning counts) is lotek
  `feat/adopt-picker-findings-count`. The scribble render degrades to "0 findings" without it.
- Schema is **Alembic-owned** since #335 (`db.create_all` → `run_migrations`); single head is now
  `c8e1a4f7b230` — chain onto it, NEVER fork a second head (silently breaks the mount, ext#169).
- **Dev/test must run from `extensions/scribble/` with `uv run --extra dev`** — scribble resolves to
  SOURCE there; the lotek-core venv imports the stale pinned package (new columns invisible).
- `seed_vuln_map` looks templates up **by name** and RAISES on a miss — so SSTI seeds before its map row.
- Tag match is deterministic: map rows ordered by id, first `row.tag ∈ finding.tags` wins.
- Prod needs the migration applied (human-gated ALTER) — note in the PR; `scribble_vuln_map` exists.
