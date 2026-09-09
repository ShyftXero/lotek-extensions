# Plan: feat/scribble-retest-threatintel

- **Branch:** `feat/scribble-retest-threatintel` (off `main`)
- **Status:** 🟡 code + tests written, scribble suite running — not yet PR'd
- **Issues:** UI-maturity sweep (lotek#621/#622 retest UI; lotek#642 threat-intel egress consent)

## Purpose

Two scribble UI holes surfaced by the extension UI/UX-maturity survey, both fronting backends that were
already written but had **no browser writer**:

1. **Retest ("verify-the-fix") had no create path — UI or machine.** `findings_service.record_retest`
   owned the outcome→status policy and the report already rendered a retest-closeout section, but
   `record_retest` had **zero callers**, so a retest round could never be entered and the report's
   remediation-status table was permanently empty.
2. **Threat-intel (KEV/EPSS) enrichment was unreachable.** `Engagement.threat_intel_egress_consent`
   defaults `False` and gates `enrichment.egress_consented`; nothing ever set it `True`, so the report's
   KEV/EPSS columns stayed NULL forever.

## Done

- `engagement_ui.py`: new `POST /findings/<id>/retest` route (`record_finding_retest`) → the single
  writer `findings_service.record_retest`; write-gated (explicit `host_can_write()` + the blueprint-wide
  `authz._gate`), 404 on missing, 400 on unknown outcome. `finding_detail` now passes `retests` +
  `retest_outcomes`.
- `finding.html`: "Verify the fix" card — retest-history table + a write-gated record form (outcome /
  date / tested-by / notes), empty state, host-CSS-token styling (no bespoke palette).
- `engagement_ui.py::_apply_engagement_form`: the consent checkbox is the ONE writer of
  `threat_intel_egress_consent` (checked = opt in, absent = clear).
- `engagement_edit.html`: opt-in checkbox with an "off by default / sends CVEs to public feeds" note.
- Tests: `test_retest_ui.py` (7 — remediated→fixed, not_tested no-op, unknown→400, missing→404,
  viewer→403, card render + viewer form-gating) and 2 in `test_engagement_crud_routes.py` (consent
  toggle + edit-page reflection).

## Remaining

- Run the full scribble suite green; `/security-review` + `/adversarial-reviewer`; PR.
- (Out of scope, noted) a machine-API retest endpoint — the model docstring still claims a machine
  caller that does not exist. UI is the deliverable here.

## Notes / gotchas

- Scribble forms use the authz cookie/PAT gate, **not** CSRFProtect — the retest form matches (no csrf
  macro), unlike bugreport's `csrf()` macro.
- Pre-existing pyrefly errors at `engagement_ui.py:605,643` (`findings_ns.list_findings` on Optional) are
  untouched by this diff — do not "fix" them here.

## Evals

- **Hypothesis:** a browser writer can record a retest round that (a) persists, (b) transitions status
  through the single writer, and (c) a viewer cannot; and an operator can opt an engagement into KEV/EPSS
  enrichment from the edit form. **Mode:** 1 (additive; both backends pre-existed).
- **Graders:** scribble's own suite (`uv run --extra dev pytest -q`) + the 9 new tests above.
- **Baseline:** before this branch, `grep record_retest scribble/` = zero callers;
  `grep 'threat_intel_egress_consent =' scribble/` = only the model default. After: one caller each,
  both behind the UI.
