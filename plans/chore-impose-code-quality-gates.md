# Plan: chore/impose-code-quality-gates

- **Branch:** `chore/impose-code-quality-gates`  (worktree: `~/tmp/ext-gate`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge (pending human review + `gh pr create`)

## Purpose

Port lotek core's `rails_gate.py` commit/PR-gate machinery onto lotek-extensions so this repo
enforces the same bar: lint+type clean at commit time, and a reviewed/tested/transcribed PR at
`gh pr create` time — with no CI here to fall back on. lotek core's gate was the source of truth
(`~/Dropbox/code/lotek/.claude/hooks/rails_gate.py`, 791 lines); this repo's prior gate only had
3 rules (explicit-staging, no-commit-on-main, ruff-clean).

## Done

- [x] Split the gate into `_rails.py` (shared, repo-agnostic utilities: quoting/segment parsing,
      git plumbing, JSONL audit log, override tokens — ported near-verbatim from lotek core) +
      `rails_gate.py` (dispatch + CLI), mirroring lotek core's own file split.
- [x] Kept the 3 existing commit rules: `explicit-staging`, `protected-branch` (no-commit-on-main),
      and ruff-clean (now scoped to staged files across subprojects — unchanged from before).
- [x] Added `pyrefly`-clean at commit time, run PER SUBPROJECT (`uv run --extra dev pyrefly check
      <files>` from that subproject's own directory — each of cream/registrar/scribble/vector has
      its own venv/lock and declares `pyrefly` under `[project.optional-dependencies].dev`, not a
      shared root project). Verified it actually catches a real type/import error (not just that
      it's wired) — see Notes.
- [x] Added the `gh pr create` gate: `--ack-review`, `--ack-adversarial` (both with a `--staged`
      pre-commit mode), `--ack-tests`, `--ack-transcripts` (required only when the branch touches
      some subproject's `tests/` dir — generalized from lotek's fixed `tests/` prefix since tests
      live at `<ext>/tests/` here, not at the repo root). Docs-only branch exemption (all-`.md`
      diff) ported verbatim, scoped to `--ack-tests` only.
- [x] **Intentional divergence, documented, not faked:** no `--ack-invariants` REQUIRED gate — this
      repo has no local `INVARIANTS.md` / `pytest -m invariant` contract (lotek core owns it).
      `--ack-invariants` is kept as a CLI for muscle-memory parity, but it only scans the four
      subprojects for `@pytest.mark.invariant` usage (none exist today) and logs an honest SKIP to
      the audit trail — never a marker, never required by `_PRE_PR_REQUIREMENTS`.
- [x] Added the `invariant-pointer` gate: a non-blocking `context` reminder on every `gh pr create`
      pointing at lotek core's `INVARIANTS.md` (resolves a local path if one of the usual checkout
      locations exists — it found `~/Dropbox/code/lotek/INVARIANTS.md` on this machine — else the
      canonical GitHub URL). Fails soft/open if lotek core isn't present; never a hard dependency.
- [x] CLAUDE.md updated: "The commit + PR gate" section rewritten (gate list, marker workflow, what
      was NOT ported and why), "Invariant divergence" explained, and a new "Security-invariant
      contract lives in lotek CORE, not here" section making the INVARIANTS.md pointer explicit —
      including the rule that every inline `INV-…` tag in this repo's code must name a REAL entry
      in lotek core's registry.
- [x] Self-tested every marker CLI (`--ack-review`, `--ack-adversarial`, `--ack-tests`,
      `--ack-transcripts`, `--ack-invariants`) — markers land in the worktree's git-dir
      (`.git/worktrees/ext-gate/claude-*.json`), correctly HEAD/tree-bound, correctly read back.
- [x] Proved the `gh pr create` gate can actually DENY: removed the adversarial-review marker,
      simulated `gh pr create` via a crafted PreToolUse JSON payload on stdin → denied with the
      exact missing-marker message; restored the marker → allowed (with the invariant-pointer
      context note attached). Red-then-green, not just "looks wired".
- [x] Proved the NEW pyrefly gate can actually DENY: staged a scratch file with a real
      `missing-import` + `unknown-name` error in `scribble/` → both ruff AND pyrefly denied the
      simulated commit with the real tool output; fixed the file → allowed. Scratch file was never
      committed (unstaged + deleted before the real commit).
      Also sanity-re-checked the 3 pre-existing rules (bulk-add deny, main-branch deny,
      `RAILS_OVERRIDE=1` bypass + audit log entry) still behave correctly after the port.
- [x] `ruff check` + `uv run pyrefly check` clean on both new hook files themselves.

## Remaining

- [ ] Open the PR (deliberately left to the human/coordinator session — this branch stops at
      pushed, per instruction). PR body should paste in the red/green proof above.
- [ ] Whoever opens the PR must re-run `--ack-review`/`--ack-adversarial`/`--ack-tests` (and
      `--ack-transcripts`, since this branch touches no `tests/` — check first) against the ACTUAL
      final HEAD; the markers recorded during this session's self-test are bound to a pre-commit
      tip and go stale the moment the real commit lands.

## Notes / gotchas

- **`--extra dev` is required for `uv run pyrefly`/`pytest` in every subproject** — they're declared
  under `[project.optional-dependencies].dev`, NOT `[dependency-groups]`, so a bare `uv run pyrefly
  check` in a fresh worktree/venv fails with `Failed to spawn: pyrefly` (not installed). Found this
  by actually running it in the fresh `.claude/worktrees` venv, not by reading the pyproject and
  assuming. Fixed in the gate code + CLAUDE.md examples.
- **pyrefly's "basic" preset does not flag every return-type mismatch** (e.g. `def f(x: int) -> str:
  return x` passed with 0 errors) — don't assume a narrow annotation mismatch alone proves the gate
  is wired; the red-then-green proof in this plan used a `missing-import` + `unknown-name` error,
  which pyrefly does catch reliably at this strictness level.
- **This session's own PreToolUse hook is lotek CORE's copy**, not this repo's (the executing
  hook is always `$CLAUDE_PROJECT_DIR`'s copy — see lotek's memory note on this). So every real
  `git commit` this session runs against `~/tmp/ext-gate` is actually evaluated by lotek core's
  gate, which resolves branch/staged-diff correctly via its worktree-aware `_effective_cwd`, but
  its `clean-checks` step runs `ruff`/`pyrefly` against `ctx.project_dir` (`CLAUDE_PROJECT_DIR` or
  the ambient shell cwd) — i.e. lotek core's OWN tree, not this repo's staged files. That's a
  pre-existing cross-repo quirk of lotek's gate, not a defect introduced here; it only means the
  self-tests in this plan (which invoke `.claude/hooks/rails_gate.py` directly via stdin JSON or a
  `cd`-then-run, bypassing the PreToolUse hook chain entirely) are the trustworthy proof, not "did
  the real git commit go through" — the real commit is additionally gated by lotek core's own
  rules, which is expected and unavoidable when working this repo from inside a lotek session.
