# fix/affected-target-url-parent-fallback

- **Status:** releasing (fast path per Eli)

## Purpose
On a parent (aggregation) finding, {{AFFECTED}} and {{TARGET_URL}} rendered BLANK in the report. Root
cause: AFFECTED's scalar url/control_id rules hit distinct child values -> _aggregate returns a
"don't-guess" '' that short-circuits _synthesize_one before the {field: target_host} fallback; TARGET_URL
has no host fallback at all and the parent has no own target_url. TARGET_HOST (host/list shape) IS
populated with the synthesized host list.

## Fix (display-time, resolver.build_context)
If AFFECTED/TARGET_URL are empty but TARGET_HOST is set, fall back to the host list — so the write-up
renders the affected hosts, not a blank. Fills empties only (real values preserved). Fixes EXISTING
findings on next render (reads the stored overlay; no re-map needed).

## Follow-up (filed separately)
Deeper synthesis fix: AFFECTED should aggregate affected URLs as a LIST, not short-circuit on distinct
scalars; TARGET_URL on a parent should list children's urls.

## Done
- resolver.build_context host-list fallback; 2 unit tests; 73 related green; ruff clean.
