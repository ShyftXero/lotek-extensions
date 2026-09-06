# Plan: feat/642-scribble-threat-intel-driver

- **Branch:** `feat/642-scribble-threat-intel-driver`  (worktree: `.claude/worktrees/s642-threat-intel-driver`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
The SCRIBBLE half of #642 threat_intel (KEV/EPSS) enrichment. The consume side already exists
(`metadata.build_threat_intel` / `threat_intel_display`, rendered guarded in reporting). This branch
adds the DRIVER that PRODUCES the `feed` from the host's `verdicts_for_cves` hook and calls
`build_threat_intel`, plus the consent / audit / degradation / tenancy plumbing.

SCAFFOLDING — the live feed is STUBBED on the core side (no egress). This side is built to the FIXED
contract `verdicts_for_cves(cves) -> (feed, health)` and exercised with hand-written mock feeds.

## The contract (built to, not reimplemented)
- `verdicts_for_cves(cves: list[str]) -> (feed, health)` from the host hook (core stub: `({}, not-wired)`).
- `build_threat_intel(cve_ids, feed, *, as_of, source) -> dict|None` — already exists; the driver calls it.

## Evals
- **Hypothesis:** with a per-engagement egress consent (default OFF, forced OFF internal), the driver
  produces a threat_intel snapshot ONLY when consented + a feed answered, records one audit row per
  lookup carrying the health map, and NEVER writes a false "no threat" on degradation.
- **Mode / aggression:** 2 (recorded experiment — security-sensitive egress-consent path)
- **Capability evals** (must newly pass): `uv run --extra dev pytest scribble/tests/test_threat_intel_driver.py -q`
  - [x] (a) consent ON + mock feed -> threat_intel populated per build_threat_intel
  - [x] (b) exploiteer ABSENT (no hook) -> None, no error, no audit
  - [x] (c) consent OFF (default) -> None, NO lookup, NO audit
  - [x] (d) degradation: health unavailable -> threat_intel None (not clean) + audit records health
  - [x] (e) internal engagement -> consent forced OFF (no lookup, no audit) even with the flag set
- **Regression evals** (must keep passing): full `scribble/tests` + single-alembic-head guard.
- **Graders:** `uv run --extra dev pytest scribble/tests -q`; ruff; pyrefly.
- **Verdict:** green-after; red-before was ImportError (`ThreatIntelDriver`/`egress_consented` absent).

## Done
- [x] `Engagement.threat_intel_egress_consent` column (Boolean, default OFF) + alembic migration off head `b8e4d2f6a130`.
- [x] `enrichment.egress_consented(engagement)` — the ONE consent predicate (default OFF, forced OFF internal).
- [x] `enrichment.ThreatIntelDriver` — `propose(db, finding)` (consent+hook-gated lookup, audits per lookup, degrades to None) + `apply(db, finding, snap)`.
- [x] `host.verdicts_for_cves()` accessor (None when unmounted -> driver degrades).
- [x] `testing.wire_mock_host(..., verdicts_for_cves=...)` injection.
- [x] `tests/test_threat_intel_driver.py` — the 5 cases, red-before/green-after.

## Remaining
- [ ] Reviews + acks + PR.

## Notes / gotchas
- Blade's map said `propose(finding) -> dict|None`; the audit must record the health of every LOOKUP
  (INV-EGRESS-02 / DEPLOY-02), and the audit seam needs a session, so the signature is `propose(db, finding)`.
- The existing `EnrichmentDriver` ABC is severity-promotion (`lookup(product,version)`); the threat-intel
  driver is a DIFFERENT shape, so it is a sibling class, not a distortion of that ABC.
- Live egress is core-side STUBBED (no egress). INV-EGRESS-01 (config-fixed destination) is a property
  of the deferred live path, not exercisable in scaffolding; the consent (EGRESS-02) + degradation
  (DEPLOY-02 / DATA-07) properties ARE exercised here. No enumerated degradable-controls guard exists
  in either repo yet (that DEPLOY-02 proving test is still owed), so there is nothing to register into.
