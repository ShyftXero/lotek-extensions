# Plan: feat/engagementfinding-refs-metadata-columns

- **Branch:** `feat/engagementfinding-refs-metadata-columns` (scribble, off ext `main`)
- **Tracking:** lotek#639 (build issue) — executes map lotek#616 decisions lotek#624 + lotek#625
- **PR:** not opened yet (ext DRAFT PR into lotek-extensions `main`)
- **Status:** 🟡 in progress

## Purpose
Promote the `references` and CVE/CWE/OWASP metadata that #617 left living only in the verbatim
`source_facts` snapshot up to TYPED columns on `EngagementFinding`, per the disposition registry —
closing two Class-2 superset leaks and giving the report renderer real reference links + metadata chips.

- **#624 references:** a JSON list of `{label, url, source, suppressed}` value objects (NOT a child
  entity). Promote UNIONS the matched library template's `references` + the scan `DTO.references`, deduped
  by normalized url, source-tagged (`template`/`scan`/`author`); operator wins on collision. Per-reference
  `suppressed`. Omit-when-empty labeled-link render block; `source` stored, hidden by default. Configurable
  columns OUT (YAGNI).
- **#625 metadata:** four ADDITIVE fields — `cve_ids`/`cwe_ids`/`owasp_categories` (typed JSON lists) + a
  dated `threat_intel` snapshot (`{as_of, source, cves:{kev,epss}}`), NOT bare kev/epss columns. Promote
  seeds `cve_ids`←`DTO.cve`, `cwe_ids`←`DTO.facts["cwe"]` (no DTO widening); `owasp_categories` DERIVED
  from `cwe_ids` via a static offline CWE→OWASP-Top-10-2021 map. Render: chips in `finding-badges` + CWE/CVE
  index columns, omit-when-empty (unenriched render byte-identical).

Re-promote stays FILL-NULL-ONLY: `promote_job` refreshes only the verbatim `source_facts` on an
already-promoted row and never touches a typed column, so an operator-suppressed/edited reference or
metadata edit is never clobbered (#617 Q5). New columns are stamped at CREATE time, like #617's
`confidence`/`status`.

## Evals
- **Hypothesis:** references + CVE/CWE/OWASP that #617 preserved only in `source_facts` now land on typed
  `EngagementFinding` columns at promote time, dedupe/derive correctly, render omit-when-empty, and an
  unenriched finding renders byte-identically to `origin/main`.
- **Mode / aggression:** 2 (before/after on the scribble suite) / 1 (realistic — real promote through the
  machine route, synthetic `FakeFindingDTO` INPUTS only, never a faked parsed fact/rendered report).
- **Capability evals** (must newly pass):
  - [x] promoted DTO with matched-template ref + scan `DTO.references` → deduped, source-tagged union —
        `tests/test_references_promote.py::test_template_match_promote_unions_template_and_scan_refs`
  - [x] all-suppressed refs → References block OMITTED — `tests/test_references_render.py`
  - [x] promoted nuclei-shaped DTO (`cve`+`facts["cwe"]`) → cve_ids/cwe_ids/owasp derive + render chips —
        `tests/test_references_promote.py` + `tests/test_references_render.py`
  - [x] `threat_intel` from a SYNTHETIC feed → KEV chip w/ `as_of`; absent feed → no chip, no error —
        `tests/test_finding_metadata.py`
- **Regression evals** (must keep passing):
  - [x] disposition drift guard passes EXACTLY (`references`/`cve` flipped to `home=column`; field set
        unchanged) — `tests/test_finding_dto_disposition_drift.py`
  - [ ] full scribble suite — baseline recorded below; candidate must add no NEWLY-BROKEN test
- **Graders:** `uv run --extra dev pytest -q` (scribble suite, before/after), the disposition drift guard,
  ruff/pyrefly clean. No LLM judge (nothing subjective).
- **Baseline (`origin/main`, ext):** RED — the two Alembic heads (`f0a1…` #620 + `f4c9…` #617, both off
  `76a1…`) were merged without a merge migration, so `run_migrations`' `stamp("head")`/`upgrade("head")`
  raises `Multiple heads are present` and EVERY app-booting test ERRORs. (Confirmed on the unchanged tree.)
- **Verdict (candidate = this branch):** the merge migration `a7d2c4e6f810` unifies the two heads AND adds
  the columns, so the mass multi-head ERRORs become PASS (a large FIXED bucket that is a genuine repair,
  not my regression). New capability tests pass; the drift guard still passes; ruff + pyrefly clean.
  (Full-suite candidate counts pasted in the PR body.)

## Done
- [x] `scribble/metadata.py` — normalize CVE/CWE, static CWE→OWASP-2021 map, `derive_owasp`,
      `coerce_reference`/`merge_references`/`visible_references`, `build_threat_intel`/`threat_intel_display`.
- [x] `EngagementFinding` columns: `references`, `cve_ids`, `cwe_ids`, `owasp_categories`, `threat_intel`;
      `from_template` seeds references from `template.references`.
- [x] Alembic `a7d2c4e6f810` — MERGE revision (unifies the two heads) + additive columns.
- [x] `dispositions.py` — `references`→`column/references`, `cve`→`column/cve_ids`; docstring updated.
- [x] `promote.py` — stamp references (template ∪ scan, deduped) + cve_ids/cwe_ids/owasp on every row.
- [x] `context.py` `FindingCtx` + `_finding_ctx` — carry references/cve/cwe/owasp/threat-intel display.
- [x] `render_html.py` — chips, omit-when-empty References block, CWE/CVE index cols, KEV flag; suppress
      legacy prose `references` block; CSS.
- [x] `render_docx.py` — parity: Classification line + References list in the body richtext.
- [x] `api_pat.py` — author references (add/edit/suppress) + cve_ids/cwe_ids/owasp + threat_intel clear;
      serialize the columns in `_finding_detail`; schemas updated.
- [x] tests (new: metadata/promote/render; updated: authoring/findings_crud) + drift guard green.
- [x] CONTEXT.md glossary: reference value object / source / suppress; finding metadata; threat-intel.

## Remaining
- [ ] Gates: `/security-review`, `/adversarial-reviewer`, full scribble suite, then `--ack-*` (bound to
      the ext HEAD, run from the submodule checkout where `is_submodule` is True), then the ext DRAFT PR.

## Notes / gotchas
- **ONE PR = the ext PR** (lotek-extensions). No core change needed: `FindingDTO.references`/`.cve`/`.facts`
  already exist. The core build issue lotek#639 is the deconfliction lock only; its board is moved by hand
  (cross-repo `Fixes` does not auto-close a lotek issue from a lotek-extensions PR).
- **Two Alembic heads on ext main were a LIVE pre-existing bug** — this branch's merge revision repairs it.
- **`threat_intel` LIVE population is deferred:** KEV/EPSS come from the exploiteer extension, which
  scribble cannot import and no host seam exposes today. Per #625 the driver degrades to None when absent,
  so this PR ships the column + render + author-clear + a PURE snapshot builder (graded with a synthetic
  feed). Wiring the live exploiteer feed lands when a host seam exists.
- **References one home:** the legacy prose `references` content block is dropped (create/PATCH no longer
  fold it; renderers skip it); references render from the structured column only. Machine-authored
  templates stay excluded from promote auto-resolution (INV-EXT-02) — the template-ref union runs on
  human-authored templates.
- **Gate mechanics:** the session PreToolUse hook is CORE's `rails_gate.py`; on `gh pr create` from INSIDE
  the submodule it detects `is_submodule` and requires ONLY `--ack-review` + `--ack-adversarial`.
