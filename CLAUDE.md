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

- UUIDv7 surrogate PKs; cross-core refs are `sqlalchemy.Uuid` (or scribble's `SoftHostId`), **never
  Integer/String** — see the trap below.
- No authorization data in the extension — resolve engagement rights through the host seam
  (`can_operate_on` / `visible_engagement_ids`), never a request body.
- Own tables, prefixed. Confirm-tier outward actions are staged + audited server-side.
- The `INVARIANTS.md` ratchet lives in lotek; an extension's invariants are proven by its lotek-side
  tests (`test_<ext>_extension.py`).

### 🔴 A PAT write 403s on your machine route — `g.principal` and the host seam

```
403 {"error":"forbidden","detail":"not an operator on this engagement"}
```

…for a token whose user genuinely HOLDS an operator membership. Every host predicate an extension
authorizes with — `can_operate_on`, `can_view_engagement`, `visible_engagement_ids` — resolves the caller
from **`g.principal`** and **fails closed** when it is absent. Core's `api_v1._authenticate_pat` sets it,
but that `before_request` fires only for `/api/v1`; an extension blueprint authenticates through
`host_contract.pat_authenticate`, which until 2026-08-14 published only the `g.api_*` scalars. Result: a
PAT could authenticate to `/<ext>/machine/*` and pass the scope gate, then be refused every write
regardless of its memberships.

Fixed in lotek (`pat_authenticate` now also builds `g.principal`), but the lesson generalizes:

- **A stub host proves your logic, never the mount.** Each extension's own suite injects its own extras,
  so a seam gap like this is invisible here and only appears MOUNTED in lotek. Always land a mounted
  test in `lotek/tests/test_<ext>_extension.py` for anything that authorizes.
- **This failed in the SAFE direction** (refuse, never leak), which is exactly why no test caught it.
  A green suite is not evidence that an authorization path is reachable — assert the ALLOWED case too.

### 🔴 `cannot cast type uuid to integer` — the core-ref column trap

```
sqlalchemy.exc.ProgrammingError: (psycopg.errors.CannotCoerce) cannot cast type uuid to integer
```

An extension column holding a **core (host) id** was declared `Integer` (or `String`). Core v2 keys every
surrogate PK on UUIDv7, so the value is a `uuid.UUID`. This is INV-INTEGRITY-03's exact red path, and it
bit `scribble_findings.source_finding_id` + `.asset_id` for real (fixed 2026-08-14).

**It hides in plain sight**: SQLite stores the value silently — only real Postgres refuses it. A green
unit run against SQLite proves nothing about this class of bug.

Use `SoftHostId` (`scribble/db.py`): a TEXT-backed `TypeDecorator` that round-trips the ORIGINAL Python
type (int for a legacy/standalone host, `uuid.UUID` for v2). There is **no** `ForeignKey` to a core table —
the referenced table isn't known until mount time. Grep before shipping:

```sh
grep -rnE "(_id|_by)\s*:.*mapped_column\((Integer|String)" <ext>/*/models.py
```

Two follow-on gotchas once you change a declared type:

- **`create_all` never retrofits an existing column's type** — it only ADDS columns. Recreate the table
  (or migrate); a pre-existing table keeps the old native INTEGER and keeps failing.
- **A non-editable `uv` path install is cached by version** — after editing source, `uv sync` reports
  "Audited" and does NOT rebuild. Force it: `uv sync --reinstall-package <ext>`.

### 🔴 Developing against a local checkout: non-editable path sources only

An extension mounts only if the host can read its `lotek-extension.toml` via
`importlib.resources.files("<pkg>")` — and that file is copied into the package by hatch
`force-include` **at wheel-build time**. An `editable = true` path install skips the build, so the
manifest is absent, `discover_extensions` **silently skips the extension**, and nothing mounts (the entry
point still resolves and `import <pkg>` still works, which makes this baffling to debug — discovery
swallows every exception by design). In lotek's `[tool.uv.sources]`, for local dev:

```toml
scribble = { path = "../lotek-extensions/scribble" }   # NOT editable = true
```

Revert the override and re-sync to the **released tag** before the final gate run, or the suite proves
unreleased code.

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
