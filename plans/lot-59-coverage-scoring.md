# Plan: lot-59-coverage-scoring

- **Branch:** `lot-59-coverage-scoring`  (off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
LOT-59 (LOT-46/A7). Turn "we found paths" into a defendable number against the seed's answer key.
Add a PURE coverage scorer that labels every DESIGNED path (the grader's answer key) with one verdict in
the `found / ran-clean / could-not-run` vocabulary, so a dead leg on a randomized seed reads as coverage,
never as "nothing exploitable" (INV-DATA-08).

## Done
- [x] `exploiteer/coverage.py` (PURE): `score_coverage(paths, chains, answer_key, *, executed=None) -> CoverageReport`.
      `DesignedPath` is the injected answer-key contract; `CoverageReport`/`PathVerdict` carry the labels.
- [x] Verdict vocabulary `found / ran-clean / could-not-run`. Absent-from-discovered -> `could-not-run`;
      `ran-clean` reachable ONLY with explicit `executed=` armed-run evidence, so plan-only analysis can
      never mislabel an absent leg (structural guard behind INV-DATA-08).
- [x] HARD RULE kept structural: `coverage.py` takes the answer key as a PARAMETER and never reads
      `/_manifest`. No I/O, no network, no clock -> deterministic.
- [x] `tests/test_coverage_scoring.py`: hermetic, fixture answer key. 7 tests green. Every designed path
      labelled; a designed path absent from the discovered set scores `could-not-run` never `ran-clean`;
      all-dead seed never renders as "nothing exploitable"; deterministic re-run.
- [x] `ruff` clean, `pyrefly` clean, `pytest -q` green.

## Remaining
- [ ] Cross-repo harness wiring lives in `ShyftXero/lotek` (NOT this repo): read `/_manifest?token=...`
      (`targets/webrange/app.py`) + `FOOTHOLDS` (`tests/busybody/fondue.py`), translate to `DesignedPath`
      list, inject into `score_coverage`, and run the §6.4 plan-assert acceptance. Filed as a follow-up
      child issue under LOT-46. Out of scope for this extension PR.

## Notes / gotchas
- `score_coverage` matching is host-keyed token overlap: a `DesignedPath` matches when its host has
  discovered tokens AND its goal (if set) is present AND its signature (if set) overlaps. A same-host+goal
  coincidence on the wrong vector is NOT a false `found` (test_signature_mismatch...).
- Graph `RankedPath` goal nodes map to the engine vocab: `("host", h)` -> `foothold` on `h`;
  `("service", h, port)` -> `service` + port. A `lateral-inferred` edge adds a `lateral` token.
- `executed=` is an optional keyword (documented 3-arg call from the ticket works verbatim); it keeps
  `ran-clean` a live, testable verdict for the future operator-gated armed run without touching the arm gate.
