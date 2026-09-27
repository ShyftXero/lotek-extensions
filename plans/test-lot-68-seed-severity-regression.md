# test/lot-68-seed-severity-regression — pin name->severity for the default vuln templates

- **Status:** in review — test-only addition to `scribble/tests`, suite green, guards proven
  fail-before/pass-after. Follow-up to LOT-49 (PR #262). Branch off `origin/main`.

## Purpose

The seed guards in `scribble/tests` pin record count, token vocabulary, and rendering, but nothing
asserted a default vuln template imports at a specific severity. That gap was not theoretical: LOT-49
(PR #262) silently moved **SMBv1 Enabled** and **Outdated or Vulnerable Software Component** from high to
medium, the suite stayed green, and only human review caught it (both reverted to high). This adds
explicit name->severity pins so a future seed edit cannot silently re-escalate or de-escalate a default.

## Done

- Added three tests to `scribble/tests/test_seed_content.py`:
  - `_EXPECTED_LOTEK_SEVERITY`: hardcoded name->Severity map for all 19
    `lotek_vulnerabilities.json` defaults (SeverityId per FACTION scale: Crit=5 High=4 Med=3 Low=2/1
    Info=0). Hardcoded on purpose — deriving from the guarded file would defeat the guard.
  - `test_default_template_name_set_is_pinned` — pinned map keys must equal the shipped Name set, so
    adding/removing/renaming a default trips first and forces a matching pin.
  - `test_default_templates_import_at_pinned_severity` — every default's `default_severity` matches
    its pin (catches any drift across all 19).
  - `test_smbv1_and_outdated_component_stay_high` — explicit redundant named pin for the two LOT-49
    slipped; survives a careless map edit.
- Verified: full seed suite green (`tests/test_seed_content.py`, `tests/test_vuln_map_seed.py`).
- Proven fail-before/pass-after: patched SMBv1 SeverityId 4->3 in the seed JSON, both severity guards
  failed with a clear message; reverted, suite green.
- `ruff` clean, `pyrefly` 0 errors on the changed file.

## Remaining

- Open PR into `main` (`--merge`/`--rebase`; squash disabled). After merge (auto release-tag), no lotek
  re-pin strictly required for a test-only change, but pin on the next scribble bump per usual flow.

## Notes / gotchas

- Test-only change, no production/seed data touched. Passes on `origin/main` as-is (main already ships
  both reverted templates at high 4).
- The pin is independent of the seed source by design; if a default is legitimately re-severitied,
  update BOTH the map (with its SeverityId comment) and, if it is SMBv1/Outdated, the targeted test.
