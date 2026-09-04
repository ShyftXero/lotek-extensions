# Plan: feat/scribble-severity-override-620

- **Branch:** `feat/scribble-severity-override-620`  (worktree: `.claude/worktrees/severity-override-620`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Implements the locked decision from lotek#620 (grilling): let an operator **manually override** a
report's computed **overall risk band** with a mandatory rationale. The computed band is a *fact* and the
override is an *authored judgement layered on top of it* — the computed value is never destroyed; the
banner shows the adjusted band + an "assessor-adjusted" marker + the original computed band + the
rationale. Direction is unrestricted (up or down); the visible marker + shown computed value + required
rationale are what keep a down-shift honest. See the #620 decision comment for the full spec.

## Evals (declared before the code — edd)
1. **Regression:** `risk_override IS NULL` → the render is unchanged (DOCX byte-identical; HTML
   markup-token-identical — the banner CSS block is always inlined, so the test asserts on markup-unique
   tokens, not on the always-present `.risk-<band>` stylesheet rules). The override code paths are
   dormant with a default of `None`.
2. **Override render (HTML):** banner shows effective band + `assessor-adjusted` marker + `computed: <orig>`
   + the rationale text.
3. **Override render (DOCX):** `overall_label` carries the marker + `computed:`; the summary narrative
   carries the attributed rationale.
4. **Standing prose:** `tests/test_report_standing_prose.py` stays green (new prose is present-tense,
   attributed, adds no forbidden past-tense conduct claim).
5. **Write guard:** PATCH that sets `risk_override` without a non-empty `risk_override_rationale` → 400;
   clearing sets both NULL; set/clear emits an audit event.
6. **Direction:** both an up-shift and a down-shift are accepted (parametrized).

## Done
- [x] #620 grilling resolved + decision spec recorded on the issue; map lotek#616 threaded.
- [x] Worktree cut off `origin/main`, identity + gpgsign set.
- [x] Scouted every edit site against the PR base.

## Remaining
- [ ] Model: two nullable columns on `Engagement` (`risk_override` `Enum(Severity)`, `risk_override_rationale` `Text`).
- [ ] Alembic revision (down_revision = `76a1de5a7c83`) — idempotent `op.add_column`, `sa.Enum(..., create_type=False)` (the `severity` PG type already exists).
- [ ] `ReportContext`: two additive `None`-default fields; populate in `build_report_context`.
- [ ] `render_html._render_summary`: effective band + marker + computed + rationale block (+ minimal CSS).
- [ ] `render_docx._build_context`: override-aware `overall`/`overall_label` + attributed rationale in the narrative.
- [ ] Machine API: `PATCH /engagements/<id>` (create-only today) + schema + validation (400 without rationale) + audit; expose both fields on `GET /engagements/<id>`.
- [ ] GUI: severity dropdown (blank = computed) + rationale textarea on the engagement settings page.
- [ ] Tests for evals 1-6.
- [ ] Follow-up (separate core PR, NOT this branch): mounted test in `lotek/tests/test_scribble_extension.py` + re-pin the scribble tag in lotek `pyproject.toml`.

## Notes / gotchas
- **Fresh DBs `create_all` from the models + `stamp head`** (`scribble/db.py:411`), so SQLite tests pick up
  the new columns automatically; the Alembic revision only matters for existing Postgres deployments.
- **`create_type=False`** on the migration's `sa.Enum` is load-bearing — the baseline migration already
  CREATEs the `severity` PG enum type; re-creating it fails `upgrade head`.
- New columns are **not** core-refs → no `SoftHostId` trap; plain `Enum(Severity)` + `Text`.
- Narrative (`_build_narrative`) reports factual **counts**, never an overall band, so it can't contradict
  the override — left untouched.
- Ext PR gate: `--ack-review` + `--ack-adversarial` + `--ack-tests` (`cd scribble && uv run --extra dev pytest -q`)
  + `--ack-transcripts` (branch touches `tests/`). Merge = `--merge` commit (squash disabled here).
