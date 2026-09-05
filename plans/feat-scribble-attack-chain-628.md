# Plan: feat/scribble-attack-chain-628

- **Branch:** `feat/scribble-attack-chain-628`  (worktree: `.claude/worktrees/s628-attack-chain`, off `feat/scribble-evidence-integrity-626`)
- **PR:** not opened yet (stacked link 3 — later links base on this branch)
- **Status:** 🟢 ready to merge

## Purpose
Add an **attack-chain narrative** to a scribble report: an ordered story of how discovered findings
chain into a broader compromise. Complements the existing `EngagementDiagram` (the *visual* of a path)
with authored, per-step *prose*. Two tables (`AttackChain` + `AttackChainStep`), a `ReportContext.chains`
projection, and a new `chains` report block rendered in both HTML and DOCX.

## Done
- [x] `AttackChain` + `AttackChainStep` models (`scribble_attack_chains` / `scribble_attack_chain_steps`),
      `Engagement.chains` relationship.
- [x] Additive migration continuing the SINGLE head (`a1b2c3d4e5f6` → this), idempotent `create_table` guard.
- [x] `ChainCtx` / `ChainStepCtx` + `ReportContext.chains` (default empty → byte-identical when no chain),
      `_chain_ctxs` builder honouring `include_in_report` + order.
- [x] `render_html._render_chains` — new `chains` block; reuses `_render_diagram_item` for a chain that
      carries an optional `embed_html` snapshot.
- [x] `render_docx._append_attack_chains` — mirrors the narrative; italic pointer note for a chain's
      HTML-only embed.
- [x] Wired `chains` into `layouts.BLOCK_KEYS`, both layouts, `_NAV_LABELS`, `_toc_entries`, block dispatch.
- [x] Tests in `tests/test_report_attack_path.py` (+ a docx mirror case in `..._docx.py`), including a
      red-then-green empty-short-circuit guard.

## Remaining
- [ ] (later stack link) PAT/machine API to author chains + the dashboard UI to edit them — out of scope here.

## Notes / gotchas
- **`_render_diagram_item` reuse:** an `AttackChain` optionally carries `embed_html`/`diagram_ref`
  (same shape as `EngagementDiagram`) so the HTML block can literally reuse `_render_diagram_item`. The
  narrative *steps* are the primary content and mirror fully in DOCX; the embed is supplementary and
  HTML-only in DOCX (an italic "delivered as an interactive figure in the HTML report" note, the same
  degradation convention `_append_attack_paths` already uses). `figure_number` is left `None` for a
  chain embed — it is NOT folded into `context.number_figures`, so it renders caption-only rather than
  disturbing the report-wide figure sequence.
- Single alembic head confirmed via `alembic -c alembic.ini heads`.
