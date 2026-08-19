# Plan: fix/scribble-filename-bounds

- **Branch:** `fix/scribble-filename-bounds`  (worktree: `.claude/worktrees/e-filename`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Close #55: neither scribble artifact-upload path (machine `api_pat.py` or cookie
`artifacts_api.py`) bounds the filename width after `secure_filename()`.
`secure_filename` NFKD-expands (e.g. `'½'*222` -> 444 chars), so a long/unicode
filename can overflow the on-disk path component (`NAME_MAX=255` -> `OSError
ENAMETOOLONG` -> 500) and/or the `Artifact.filename` `String(512)` column
(`StringDataRightTruncation` on Postgres — silently truncated on SQLite, which
is why the local SQLite-only suite never caught it). Fix: bound the secured
name (preserving extension) via one shared helper, applied to the on-disk name
and the DB column value, on both upload paths.

## Done
- [x] Worktree + identity + plan doc

## Remaining
- [x] Investigated main's actual state — CORRECTED the plan's premise (see Notes)
- [x] `artifacts_storage.py`: export `SAFE_NAME_MAX` (was private `_SAFE_NAME_MAX`) — `_bounded_name`
      (which truncates the SECURED name after `secure_filename`, protecting the on-disk write from
      NFKD expansion) already exists on main and is unchanged
- [x] `api_pat.py` machine upload: already had an input-side cap (`_ARTIFACT_FILENAME_MAX_LEN`) —
      refactored to import the shared `SAFE_NAME_MAX` instead of recomputing `255-32-1` a second time
- [x] `artifacts_api.py` cookie upload: had NO cap at all — added the same length check (400) before
      any tenancy check / disk write
- [x] Tests: `test_save_bytes_bounds_name_after_secure_filename` (unit), 4 new cookie-route tests
      (reject-over-cap multipart + JSON, accept-at-cap), 2 new machine-route tests (reject-over-cap,
      NFKD-expansion-survives-on-disk) — all pass; ruff + pyrefly clean
- [ ] `cream/pyproject.toml` addopts fold-in — SKIPPED, see Notes (PR #65/#56 already owns it)
- [x] ruff + pyrefly clean on all changed files
- [ ] full scribble suite run (in progress) + adversarial-reviewer + security-review, ack markers
- [ ] PR opened, issue #55 linked

## Notes / gotchas
- **Corrected the plan's premise after reading main directly.** `_bounded_name` (the fix for
  residual #1 — `secure_filename` NFKD-expansion overrunning `NAME_MAX`/the on-disk write) was
  ALREADY present in `artifacts_storage.save_bytes` on origin/main (landed in #41/#60,
  `feat/scribble-machine-findings-crud`), and `api_pat.py` already had an input-side length cap
  (`_ARTIFACT_FILENAME_MAX_LEN = 222`) that protects its write of `Artifact.filename` (String(512))
  from overflow too. So residual #1 in the issue is already fixed on main for BOTH the filesystem
  and the machine route's DB column. **Residual #2 — the cookie route (`artifacts_api.py`) had
  ZERO filename bound at all** — was the only real gap, and is what this branch fixes: added the
  same length check the machine route already had, sourced from one shared constant
  (`artifacts_storage.SAFE_NAME_MAX`) so the two routes' caps can't drift apart. Net diff is smaller
  than the plan anticipated because most of the described fix was already shipped.
- `secure_filename` output is pure ASCII, so a plain string slice for
  truncation is byte-safe (no multi-byte split risk) — unchanged, verified in the pre-existing
  `_bounded_name`.
- `cream/pyproject.toml` addopts `-q` fold-in from the issue is DELIBERATELY SKIPPED here: PR #65
  (branch `fix/cream-addopts-q-count`, closing #56) already lands exactly this fix. Touching the
  same file here would conflict/duplicate; left out to keep this PR focused on #55.
