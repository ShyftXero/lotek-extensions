# Plan: chore/add-gh-app-token-script

- **Branch:** `chore/add-gh-app-token-script`  (worktree: `.claude/worktrees/gh-app-token`, off `main`)
- **PR:** to be opened bot-authored
- **Status:** 🟢 ready to merge

## Purpose
Give the monorepo its own copy of `scripts/gh-app-token.py` so a session working only here can mint the
`lotek-agent[bot]` installation token (bot-authored PRs → the human is a genuine non-author approver).
Until 2026-08-04 the tool lived only in the lotek checkout, and the App was not even installed on this
repo — so the first PR here (#1) had to be human-authored and then could not be self-approved. The App
was granted access on 2026-08-04; this lands the tool locally with the setup story in its docstring.

## Done
- [x] Copy `scripts/gh-app-token.py` from lotek (code identical — the tool is already env-var portable).
- [x] Enrich the docstring: PURPOSE, USAGE (incl. the one-call mint+push+PR recipe), REPO SCOPING (why
      #1 fell back to the human + the exact App-install fix), CONFIGURATION (all env vars).
- [x] Repoint the `--check` default (`TARGET_REPO`) to `ShyftXero/lotek-extensions` for this copy.
- [x] Verified `--check` against this repo: reachable, all required perms present.

## Remaining
- [ ] Human approves the PR; squash-merge.

## Notes / gotchas
- Same App (`lotek-agent`, id 4451736) + installation (150438876) + credentials (`~/.config/lotek-agent/`)
  serve both repos. No new secrets.
- An installation token only reaches repos granted at mint time — after adding a repo, delete
  `~/.config/lotek-agent/token.json` to force a fresh mint (the script does this automatically near expiry).
- No unit test added here: the security-relevant `write_cache` guard is already covered by lotek's
  `tests/test_gh_app_token_cache_perms.py`, and this is a byte-for-byte-behaviour copy. The monorepo gate
  requires only ruff-clean + a feature branch; `ruff check scripts/gh-app-token.py` is clean.
