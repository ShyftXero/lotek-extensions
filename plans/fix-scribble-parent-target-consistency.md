# Plan: fix/scribble-parent-target-consistency

- **Branch:** `fix/scribble-parent-target-consistency` (worktree: `extensions/.worktrees/target-a`, off `origin/main`)
- **PR:** not opened yet
- **Status:** 🟢 ready

## Purpose
On a finding that aggregates many hosts (a synthesized PARENT), the finding form's **Target host / port /
URL** fields render blank while the report prose `{{TARGET_HOST}}` renders the full comma-joined host
list — the two surfaces disagree (reported on a UKG XSS finding with ~85 hosts).

**Decision A (chosen by Eli):** the Affected-hosts table is the source of truth for a multi-host finding;
the single-value Target fields should be **blank-by-design** for a multi-host parent, and **consistent**
across form + prose. A single-host parent shows that one host in both.

## Done
- [x] `report_variables.json`: `TARGET_HOST` shape `host` → `scalar` (mirror `TARGET_URL`): multi-host
      parent synthesizes to `""`, single-host to the one host. (AFFECTED keeps its `host` list — that is
      its job.)
- [x] `promote.py`: after a parent's variables are synthesized, mirror the single-value targets onto the
      parent's own columns via `_target_overrides` (writes nothing when `""`), so form == prose.
- [x] Eval/test: multi-host parent → `target_host` column None AND `{{TARGET_HOST}}` == ""; single-host
      parent → both show the host.

## Notes / gotchas
- `_target_overrides` only writes non-empty, so a multi-host parent's column stays blank (no clobber).
- Child rows are single-host; `scalar` vs `host` shape resolves a bare hostname identically (TARGET_URL
  already proves the scalar path). 
- Sits alongside the vuln-map PR (#304) which also edits promote.py but in different functions.
