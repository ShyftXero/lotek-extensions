# Plan: feat/scribble-vuln-mapping-tag-cwe

- **Branch:** `feat/scribble-vuln-mapping-tag-cwe`  (off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

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
- [x] Branch + plan.

## Remaining
- [ ] `ScribbleVulnMap`: add `tag` + `cwe_id` columns (+ migration off head `b2c4e6f8a1d3`).
- [ ] `resolve_vuln_template`: accept `tags`/`cwe`; precedence `dedupe_prefix > title > tag > cwe > source`.
- [ ] `_matched_template` + `api_pat.scribble_resolve_template`: thread `facts["tags"]`/`facts["cwe"]`.
- [ ] `seed_vuln_map`: support `tag`/`cwe` keys (idempotency match-key + construction).
- [ ] `lotek_vuln_map.json`: tag taxonomy (~15) + whole-toolchain source rows (snmp/egress/smb/…) +
      TLS specific-id refinements + `nuclei:cve-` catch-all.
- [ ] Author **SSTI** template (`lotek_vulnerabilities.json`) + map `ssti` tag → it.
- [ ] **Re-map a job** (non-destructive reparent of flats that now match) + UI button.
- [ ] Fold in the **adopt-picker findings/severity badge** (`/tmp/badge_work.patch`).
- [ ] Bug fixes: finding-page **preview `parseInt(uuid)`** regression; **target-A** (parent target
      columns stay blank on a multi-host parent, consistently).
- [ ] Tests for each; evals (baseline flat-count before/after on a synthetic nuclei job).

## Notes / gotchas
- Schema is **Alembic-owned** since #335 (`db.create_all` → `run_migrations`); single head
  `b2c4e6f8a1d3` — chain onto it, NEVER fork a second head (silently breaks the mount, ext#169).
- `seed_vuln_map` looks templates up **by name** and RAISES on a miss — so SSTI must seed before its map
  row; use the exact active template name.
- Tag match must be deterministic: order map rows by id, first `row.tag ∈ finding.tags` wins.
- Prod needs the migration applied (human-gated ALTER) — note in the PR; `scribble_vuln_map` exists.
