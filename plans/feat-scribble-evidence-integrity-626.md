# Plan: feat/scribble-evidence-integrity-626

- **Branch:** `feat/scribble-evidence-integrity-626`  (worktree: `.claude/worktrees/s626-integrity-manifest`, stacked off `feat/scribble-retest-model-621`)
- **PR:** not opened yet (agent does not self-PR; human routes)
- **Status:** 🟢 ready to merge

## Purpose
Publish an evidence-integrity SHA-256 manifest (#626). Uploaded artifacts already carry a content hash
(`Artifact.sha256`, stamped at persist time) but nothing surfaced it. Carry that hash to the two places a
client verifies against: the report's Evidence appendix (HTML + docx) and the machine artifact API. No
migration — the column exists; this only threads it through the renderers and the PAT surface.

## Done
- [x] `ArtifactCtx.sha256` (context.py, additive/defaulted) + carried in `_artifact_ctxs` — single seam
      both renderers read.
- [x] `render_html._render_integrity_manifest` — filename → SHA-256 table inside the Evidence appendix
      (no new `<section id>`, so the TOC completeness guard is untouched).
- [x] `render_docx._append_sha256_line` — SHA-256 line under each artifact in `_append_evidence_appendix`.
- [x] `api_pat._machine_artifact_dict` + `_artifact_summary` expose `sha256` (feeds #627).
- [x] Guard test `tests/test_report_integrity_manifest.py` (RED→GREEN): ctx carry, HTML manifest, docx
      line, both machine surfaces.

## Remaining
- [ ] Human review + PR + merge.

## Notes / gotchas
- Scope: the rendered manifest covers ENGAGEMENT-LEVEL evidence (`ReportContext.artifacts`), symmetric
  with the docx `_append_evidence_appendix` seam. Finding-attached evidence is not in the rendered
  manifest, but the machine API exposes `sha256` for EVERY artifact. A report-wide manifest over all
  finding galleries is a follow-up if wanted (would need a new block key + TOC/layout wiring).
- Manifest lists only rows that actually carry a hash; a legacy pre-hash row is silently absent, so an
  engagement whose evidence predates hashing renders byte-identically to before.
- Single Alembic head unchanged: `a1b2c3d4e5f6` (from base 621); this branch adds no migration.
- Full scribble suite green: 1412 passed / 11 skipped / 0 failed (incl. the 4 new tests).
