# Plan: feat/scribble-engagementfinding-superset

- **Branch:** `feat/scribble-engagementfinding-superset` (off `origin/main`)
- **PR:** <opened at gate time — DRAFT into lotek-extensions `main`>
- **Tracking:** lotek build issue #636, executing map ShyftXero/lotek#616 decision #617.
- **Status:** 🟡 in progress

## Purpose
The keystone of map #616: make scribble's `EngagementFinding` a **lossless superset** of the scan
`FindingDTO` (`host_contract.FindingDTO`, the neutral seam — scribble never imports lotek). Adds the
verbatim `source_facts` snapshot, the origin×operator disposition registry, maps `confidence`+`status`
in promote (columns existed but sat at their defaults), and a registry drift guard. Downstream tickets
(#620/#621/#624/#625/#627/#628) stack on the axes this establishes.

## Evals
- **Hypothesis:** promoting a `FindingDTO` yields an `EngagementFinding` whose `source_facts` reproduces
  every DTO field verbatim (even on the template-match path) and whose `confidence`/`status` reflect the
  DTO, with zero regression to promote/aggregation.
- **Mode / aggression:** 2 / 2 (unknown enum values, missing fields, uuid ids, template-match path).
- **Capability evals** (must newly pass):
  - [ ] `tests/test_source_facts_promote.py` — verbatim snapshot on both promote paths; confidence/status
        mapped; re-promote refreshes `source_facts` without clobbering an operator edit.
  - [ ] `tests/test_finding_dto_disposition_drift.py` — registry covers exactly the DTO fields, axes
        valid, snapshot JSON-safe, enum mapping defensive.
- **Regression evals** (must keep passing): the whole scribble suite —
  `TMPDIR=<owned> uv run --extra dev pytest -q`. Baseline recorded in the PR body.
- **Graders:** the scribble `pytest` suite (deterministic). No LLM judge.
- **Verdict:** in the PR body (baseline vs candidate counts).

## Done
- [x] `scribble/dispositions.py` — origin×operator registry, `snapshot_source_facts`, defensive
      `confidence_from_dto`/`status_from_dto`.
- [x] `scribble/models.py` — `source_facts` JSON column on `EngagementFinding`.
- [x] alembic revision `f4c9a1b2e370` (down `76a1de5a7c83`) — additive nullable `source_facts`.
- [x] `scribble/promote.py` — `_source_overrides` (confidence/status/source_facts) on both paths;
      `promote_job` refreshes `source_facts` on re-promote (fill-NULL-only for typed columns).
- [x] drift-guard test + source_facts promote test; red→green transcripts in the PR body.

## Remaining
- [ ] reviews (`/security-review` + `/adversarial-reviewer`) + `--ack-*`, open the DRAFT PR.

## Notes / gotchas
- Core `Finding.confidence`/`.status` enum VALUES equal scribble's exactly (by design), so a valid value
  maps 1:1; mapping is still DEFENSIVE (unknown -> default), mirroring `from_lotek_finding`'s severity rule.
- `source_facts` is a `sa.JSON` column; the snapshot coerces a `uuid.UUID` id to `str` so `json.dumps`
  never raises on commit.
- Fill-NULL-only is the safe reading of #617 Q5: re-promote never overwrites a non-empty typed column, so
  an operator edit can never be stomped. Typed-column fill-from-source and the change-notice/diff are
  deferred to the editability tickets (#620/#621); this branch refreshes `source_facts` only.
- The real drift guard against `app.host_contract.FindingDTO` runs MOUNTED / when a lotek checkout is on
  the path (`importorskip`); scribble's own venv has no lotek dep, so it SKIPs honestly here. Core-side
  re-pin + mounted guard is the documented lotek follow-up.
- False-RED on this box: `/tmp/pytest-of-shyft` owned by another uid — run with a private `TMPDIR`.
