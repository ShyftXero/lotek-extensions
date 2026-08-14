# Working in this repo with Claude

The home for lotek platform **extensions**. It adopts the [lotek](https://github.com/ShyftXero/lotek)
framework's flow and processes. The essentials:

## One monorepo, self-contained subdirectories

Each extension lives in its own subdir (`cream/`, `registrar/`, `scribble/`, `vector/`), carrying its own
`pyproject.toml`, `lotek-extension.toml`, and package. A change touches ONE extension's subdir unless it
is a cross-cutting repo change (this file, CI, the plan template). Keep extensions independent — an
extension reaches lotek only through the injected host contract, never by importing lotek or another
extension.

## Branch per change type — off `main` (GitHub Flow)

Cut a **short-lived** branch off `main` (`feat/` · `fix/` · `chore/`), PR it **into `main`**,
**squash-merge**, and let it **auto-delete** (`delete_branch_on_merge` is on). Don't pile new work onto a
finished-but-unmerged branch. Prefix the branch with the extension when it helps: `feat/cream-pdf`.

## One working tree per session

A git working tree has one shared `HEAD` and one index; two sessions in the same directory corrupt each
other's commits. Use a worktree per session, under `.claude/worktrees/`:

```sh
git worktree add .claude/worktrees/<topic> -b <type>/<name> origin/main
```

## Agent commits carry a split identity

`author` is the human who directed + approved the work; `committer` is `lotek-agent[bot]`, which typed it.
Set it **per worktree**, never via `user.*`:

```sh
git config extensions.worktreeConfig true
git config --worktree author.name  "Eli McRae"          # + author.email
git config --worktree committer.name "lotek-agent[bot]"  # + committer.email
```

PRs are opened with the bot's token so the human is a genuine non-author reviewer (GitHub forbids a PR
author approving their own PR).

## Per-branch plan — `plans/<branch-slug>.md`

Every branch carries a plan file in `plans/` (branch name, `/`→`-`), copied from `plans/TEMPLATE.md` and
committed FIRST. Purpose · Status · Done · Remaining · Notes. It merges to `main` with the branch as a
durable record.

## Tests + lint are mandatory

Every change ships with tests pinning expected behaviour; a bugfix adds a test that fails before and
passes after. Before opening a PR, for the extension(s) you touched:

```sh
uvx ruff check <ext>                       # clean
cd <ext> && python -m pytest -q            # the extension's own tests green (where present)
```

Prove a *mounted* behaviour (tenancy, the host seam) against lotek's suite too — an extension's real
contract is how it behaves inside lotek. See the extension's coverage in
`lotek/tests/test_<ext>_extension.py`.

## The commit gate

A PreToolUse hook (`.claude/hooks/rails_gate.py`) gates commits: it blocks `git add -A`/`commit -a`
(stage explicit paths), blocks a commit landing on `main`, and requires `ruff` clean on the staged
Python. Override once with `RAILS_OVERRIDE=1` (logged).

## v2-native contract (every extension)

- UUIDv7 surrogate PKs; cross-core refs are `sqlalchemy.Uuid`, never Integer/String.
- No authorization data in the extension — resolve engagement rights through the host seam
  (`can_operate_on` / `visible_engagement_ids`), never a request body.
- Own tables, prefixed. Confirm-tier outward actions are staged + audited server-side.
- The `INVARIANTS.md` ratchet lives in lotek; an extension's invariants are proven by its lotek-side
  tests (`test_<ext>_extension.py`).

## Shipping a change back to lotek (pinned git deps — vendoring is retired)

Extensions are **no longer vendored** into lotek. lotek installs each as a **pinned git dependency** from
this public monorepo (one subdirectory per extension), discovers it via the `lotek.extensions` entry point,
and reads its metadata from the wheel-shipped `lotek-extension.toml`. There is no vendored tree and no
`stage-extension.sh`. To ship a change:

1. **Land it here.** PR the extension's subdir into `main`, squash-merge. On merge, CI
   (`.github/workflows/release-tag.yml`) cuts a dated release tag `v<YYYY.M.D.HHMMSS+g<sha>>`.
2. **Re-pin in lotek.** Bump that extension's `tag = "…"` under `[tool.uv.sources]` in lotek's
   `pyproject.toml`, then `uv lock --upgrade-package <ext>` + `uv sync --extra extensions`, run the mounted
   tests (`tests/test_<ext>_*`, `test_extension_nav`), and PR into lotek `main`.

Never hand-edit an installed copy under lotek's environment — it is replaced on the next `uv sync`.
See `README.md` ("How lotek consumes these") for the consumer side, and the runtime loader in lotek's
`src/app/extensions.py`.
