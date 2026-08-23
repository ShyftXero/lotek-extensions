# fix/agent-push-parity

- **Status:** in review — PR open, awaiting human merge.

## Purpose
Bring lotek-extensions to parity with lotek on bot-identity pushes (companion to lotek PR #426). Without
it, a plain `git push` to this repo from an agent PRIMARY checkout authenticates as the human (SSH), making
the human the "last pusher" — which `require_last_push_approval` would then bar from approving. Agent
WORKTREE pushes here are already covered by the shared `~/.config/git/lotek-bot.inc` include (its
`includeIf gitdir:**/lotek-extensions/.git/worktrees/` stanza); this closes the primary-checkout gap.

## Done
- **`scripts/agent-push.sh`** (new): copied from lotek — mints the bot App token via this repo's own
  `scripts/gh-app-token.py` and pushes via an ephemeral `x-access-token` HTTPS remote. Repo-agnostic (derives
  owner/repo from the remote), refuses to push `main`.
- **`push-identity` gate** ported into `.claude/hooks/rails_gate.py` (`_push_args`, `_push_remote`,
  `_bot_auth_active`, `_g_push_identity`; registered in `GATES`). Byte-identical to lotek's. Denies a plain
  SSH push, allows an explicit token remote AND a transparent bot-auth worktree (`_bot_auth_active`).
- **`CLAUDE.md`**: moved `push-identity` from "not ported" to "PORTED".
- Verified: `py_compile` OK, `ruff` clean, and a functional smoke (`ext_gate_smoke.py`) — deny SSH-no-botauth,
  allow explicit-token, allow bot-auth-config, deny half-config (insteadOf without a bot helper): all pass.

## Notes
- Exhaustive unit tests for the gate live in lotek core (`tests/test_claude_hooks.py`, 17 push-identity
  cases incl. the transparent-worktree allow/deny). This repo has no hook-test harness (its other ported
  gates are likewise unit-tested only in lotek); the smoke above is the local runnable check.
- This branch was itself pushed by a plain `git push` from an ext worktree — dogfooding the transparent
  bot-auth (the includeIf covers `lotek-extensions/.git/worktrees/`).
