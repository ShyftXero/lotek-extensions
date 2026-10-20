# Plan: chore-rails-gate-resolve-uv

- **Branch:** `chore/rails-gate-resolve-uv`  (worktree: `.claude/worktrees/chore-hook-uv`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge

## Purpose
The `clean-checks` guard in `.claude/hooks/rails_gate.py` invoked `uv` by name, which fails silently in
sandboxed agent sessions where `uv` is installed outside PATH (e.g. `~/.local/bin/uv`). Additionally the
pyrefly step could fail on environment-setup errors (missing network, proxy, sandbox cache deny) and
incorrectly block commits as type errors. Fix: resolve `uv` from known install locations; prefer the
subproject's existing `.venv/bin/pyrefly` binary (no env resolution, no network); distinguish env errors
(fail open) from real pyrefly `ERROR` findings (block).

## Done
- [x] Patch `_g_clean_checks` in `.claude/hooks/rails_gate.py`
- [x] Ruff clean (`All checks passed!`)
- [x] Rebased on top of `origin/main` (one commit ahead)
- [x] Plan file committed

## Remaining
- [ ] Earn gate markers (security-review, adversarial-review)
- [ ] Open PR with bot token
- [ ] Request Plague review

## Notes / gotchas
- Only `.claude/hooks/rails_gate.py` is touched — no extension subproject, no tests needed.
- Branch is docs-only-adjacent (hook-only), so `--ack-tests` may be exempt, but this repo's gate is
  strict: earn review markers regardless.
