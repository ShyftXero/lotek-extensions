# Plan: fix/scribble-promote-cross-engagement-guard

- **Branch:** `fix/scribble-promote-cross-engagement-guard` (off `main` @ b485304)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
lotek#845 — bind a scribble **report board** to the ONE core engagement it is anchored to, and refuse
promoting a scan job that belongs to a DIFFERENT core engagement. Before this, `promote_job` never
checked the anchor, so a write-scoped operator could pour another client's engagement's findings into a
report board (silent tenancy mix; INV-INPUT/tenancy). Core engagement is the authorization boundary.

## Scope note — part of #845 already shipped upstream (#229, "report-board rework")
- ✅ **UNIQUE anchor** — `d1e2f3a4b5c6` added a PARTIAL unique index on `core_engagement_id` (non-null).
  Better than the full `unique=True` the original #845 plan proposed. **So no migration / model change here.**
- ✅ **Naming disambiguation** — the scribble `Engagement` model/table was renamed to
  `ReportBoard` / `scribble_report_boards`. That resolves #845's "two engagements" naming ask.
- ❌ **The promote anchor GUARD was NOT done** — `promote_job` was left unchanged. That is this branch.

## Done
- [x] `scribble/promote.py`: `CrossEngagementPromote` + `assert_promote_anchor(board, job_engagement_id)`
      predicate (single home) + `_norm_core_id`. `promote_job` gains a REQUIRED kw-only `job_engagement_id`
      and calls the guard FIRST (before any row op). Model-agnostic (reads `.core_engagement_id`).
- [x] `scribble/api_pat.py` (machine route): passes `job.engagement_id`, translates the exception → **409**
      `{"error":"cross_engagement","detail":"…Reassign…"}` (409 not 404: caller is authorized on both job
      and board, so naming both ids leaks no oracle).
- [x] `scribble/engagement_ui.py` (human routes): `promote_job_ui` and `adopt_job` pass `job_engagement_id`
      and `abort(409, …)`. `adopt_job` checks the anchor BEFORE its gating `mark_job_promoted`, so a refused
      adopt never leaves a job linked-but-not-poured.
- [x] `scribble/tests/conftest.py`: `StubFindings.add_job(engagement_id=…)` + `get_job` exposes it.

## Remaining
- [ ] Wire the promote-affected tests to anchor each job to its board; add guard tests (machine/human/adopt
      409 + `assert_promote_anchor` unit) — in progress (worker).
- [ ] `uvx ruff check` + per-subproject `uv run --extra dev pyrefly check` clean.
- [ ] `/security-review` + `/adversarial-reviewer` → `--ack-review` / `--ack-adversarial` (cross-repo: core's
      gate fires from this lotek-rooted session — expect `RAILS_OVERRIDE=1` on the ext PR per ext CLAUDE.md).
- [ ] `--ack-tests` after `cd scribble && uv run --extra dev pytest -q` green.
- [ ] PR (bot-authored) `Refs ShyftXero/lotek#845`, then self-merge → ext auto-cuts a release tag.
- [ ] Hand the new ext tag to the #845-core re-pin (CONTEXT.md disambiguation optional — the rename covers it).

## Evals (EDD)
- **Baseline (ext origin/main b485304):** a promote of a job whose core engagement ≠ the board anchor
  SUCCEEDS silently (findings land, mark fires). 24 promote tests green because none assert the anchor.
- **Delta (this branch):** the mismatched promote is REFUSED — 409 / abort(409), NO findings written, adopt
  NOT marked; matched-anchor promote unchanged; guard tests pin all three surfaces + the predicate.

## Notes / gotchas
- Supersedes the parked `feat/scribble-promote-anchor-guard` branch (commit `bfff8e7`), which was built off
  the pre-rename main and duplicated the UNIQUE upstream shipped. That branch is abandoned; this is the
  guard-only re-derivation against `ReportBoard`.
- The guard fails CLOSED on a missing anchor on either side (prod always has both). Tests must anchor their
  jobs — a stub returning a magically-matching id would hide the leak this guards.
