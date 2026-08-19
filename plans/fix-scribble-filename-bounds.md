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
- [ ] `artifacts_storage.py`: add `bounded_filename()` helper + `_STORED_NAME_MAX_BYTES`
- [ ] `artifacts_storage.py save_bytes()`: use `bounded_filename` (defense-in-depth)
- [ ] `api_pat.py` machine upload: bound + store `stored_filename`
- [ ] `artifacts_api.py` cookie upload: bound + store `stored_filename`
- [ ] Tests: unit `save_bytes` bound test + machine-route test + cookie-route test, each red-before/green-after
- [ ] `cream/pyproject.toml`: drop `-q` from `addopts`
- [ ] ruff + pyrefly + scribble test suite
- [ ] adversarial-reviewer + security-review, ack markers
- [ ] PR opened, issue #55 linked

## Notes / gotchas
- Bug confirmed NOT present on main today (no width cap anywhere) — issue's
  premise about `feat/scribble-machine-findings-crud` adding a cap is not
  reflected on main; still, same root fix closes #55.
- `secure_filename` output is pure ASCII, so a plain string slice for
  truncation is byte-safe (no multi-byte split risk).
- `cream/pyproject.toml` addopts `-q` fold-in is separate/minor — noted in PR
  body; a sibling worktree (`cream-addopts-q`) may already touch this file —
  check for conflict before opening PR.
