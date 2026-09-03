# Working in this repo with Claude

The home for lotek platform **extensions**. It adopts the [lotek](https://github.com/ShyftXero/lotek)
framework's flow and processes. The essentials:

## One monorepo, self-contained subdirectories

Each extension lives in its own subdir (`bugreport/`, `cream/`, `exploiteer/`, `registrar/`, `scribble/`,
`vector/`), carrying its own `pyproject.toml`, `lotek-extension.toml`, and package. A change touches ONE
extension's subdir unless it is a cross-cutting repo change (this file, CI, the plan template). Keep
extensions independent — an extension reaches lotek only through the injected host contract, never by
importing lotek or another extension.

### `kit/` is NOT an extension

`kit/` holds **`lotek-kit`**, a shared contract library (see `kit/README.md`). It is the one subdir with
no `lotek-extension.toml`, no `lotek.extensions` entry point and no `register()` — lotek's discovery
enumerates exactly that entry-point group, so their absence is what makes it structurally unmountable.

It exists because the rule above leaves two consumers who may not import each other with nowhere to put
code they both need, so they copy it instead — this repo carries three hand-written drag-reorder
implementations, and core knows the `attackpath/v1` schema id as a string literal. Core depends on the
kit, extensions depend on the kit, and the kit depends on neither.

**The admission rule: something enters the kit only when two consumers that may not import each other
both need it.** Convenience is not a reason to widen a package every consumer must install. `lotek-kit`
carries **no runtime dependencies** (core takes it as a *base* dependency, so anything added here lands
in lotek and in every extension downstream) and **never imports lotek or an extension**. All three
constraints are pinned by tests in `kit/tests/`.

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

PRs are opened with the bot's token so a PR is attributed to the agent that wrote it, not the human who
directed it. **Until 2026-08-27 this was also load-bearing for approvals**: `main` required 1 approving
review and GitHub forbids a PR author approving their own PR, so bot authorship was the only thing that
let Eli approve an agent's work. Approvals on `main` are now **`0`** on this repo and on core, so nothing
breaks if a PR is opened under human auth — keep doing it as the bot anyway, because it is what makes
turning the requirement back on a one-field change. See ShyftXero/lotek#500.

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

## The commit + PR gate

A PreToolUse hook (`.claude/hooks/rails_gate.py`) gates commits AND `gh pr create` — ported from
lotek core's `rails_gate.py` (2026-08, same author's design) so this repo enforces the same bar,
adapted for a monorepo of four independent `uv` subprojects instead of one project:

**Commit-time** (fast, runs on every `git commit`):
- `explicit-staging` — blocks `git add -A`/`.` and `commit -a` (stage explicit paths instead).
- `protected-branch` — refuses a commit landing directly on `main`.
- `clean-checks` — `ruff` clean on the staged Python (one pass across all staged files; ruff's own
  config discovery picks each file's nearest subproject `pyproject.toml`), **and now `pyrefly`
  clean, per touched subproject** (`uv run --extra dev pyrefly check <files>`, run from that
  subproject's own directory since each has its own venv/lock — `pyrefly`/`ruff`/`pytest` live
  under each subproject's `[project.optional-dependencies].dev`, so `--extra dev` is required).
  Fails OPEN (logs it) if `uv`/`pyrefly` aren't available; a real type error still denies.

**PR-time** (`gh pr create` is blocked unless every APPLICABLE marker below is recorded for the
current HEAD — there is no CI here to trust as a merge gate, so local verification before the PR
is the real one):

```sh
/security-review                                      # review git diff main...HEAD, resolve findings
python3 .claude/hooks/rails_gate.py --ack-review
/adversarial-reviewer                                 # review git diff main...HEAD, resolve BLOCK/CONCERNS
python3 .claude/hooks/rails_gate.py --ack-adversarial
# run the relevant subproject suite(s): cd <ext> && uv run --extra dev pytest -q
python3 .claude/hooks/rails_gate.py --ack-tests
# only if the branch touches some subproject's tests/ — break each guard added, watch it fail, fix it
python3 .claude/hooks/rails_gate.py --ack-transcripts
gh pr create --base main --head <branch>              # ALL applicable markers present + current
```

Both `--ack-review` and `--ack-adversarial` also accept `--staged` (ack the staged tree before your
final commit — the marker binds to `git write-tree` and survives the commit). Every marker is keyed
to `git rev-parse HEAD` (or the staged tree for `--staged`); a further commit invalidates it and it
must be re-acked. A branch whose every changed path is `.md` is exempt from `--ack-tests` only (not
review/adversarial). Override any single gate with `RAILS_OVERRIDE=1 <cmd>` (logged to
`<git-dir>/claude-rails-audit.jsonl`, same as every deny/fail-open/warn).

**`push-identity` — PORTED (2026-08-22).** A `git push` to GitHub should authenticate as the bot, never as
the human. Under the old regime this was a hard blocker — the human became the "last pusher" and
`require_last_push_approval` barred their approval — but that flag and the approval requirement are both
off since 2026-08-27, so a human-authenticated push now only misattributes the push. The
gate denies a plain SSH push and points at `scripts/agent-push.sh` (now also present here); it recognises a
transparent bot-auth AGENT WORKTREE (`_bot_auth_active`: lotek's `install-bot-push-auth.sh` wires an
`includeIf` bot-auth include scoped to `…/lotek*/.git/worktrees/`, which covers this repo's worktrees too)
and allows the plain push there. Byte-identical to lotek core's gate; its exhaustive unit tests live there.

**Still not ported (deliberately):** lotek core's `branch-owner` (shared-worktree session collision guard),
`merge-gate` (local `git merge` into main), and the `regression-test`/`ci-required-checks` soft advisories —
add those later if this repo grows the same multi-session pressure lotek did.

### Invariant divergence (intentional) — this repo has NO local invariant contract

lotek core has a deterministic `INVARIANTS.md` + `pytest -m invariant` suite (~150 tagged tests) and
a non-opt-in `--ack-invariants` gate that RUNS that suite and only records a marker if it's green.
**This repo has no equivalent** — see "Security-invariant contract" below: the registry and its
enforcing tests both live in lotek core, not here. Porting `--ack-invariants` as a REQUIRED
`gh pr create` gate in this repo would fake a green check for a contract this repo cannot itself
run. So `--ack-invariants` exists here only as a CLI (muscle-memory parity with lotek) that scans
the four subprojects for `@pytest.mark.invariant` usage (there is none today) and logs an honest
SKIP-with-reason to the audit trail — never a marker, never a required gate. The compensating
control is the `invariant-pointer` gate below: a non-blocking reminder on every `gh pr create`.

## Security-invariant contract lives in lotek CORE, not here

The canonical security-invariant contract is lotek core's `INVARIANTS.md`
(https://github.com/ShyftXero/lotek/blob/main/INVARIANTS.md; locally usually
`~/Dropbox/code/lotek/INVARIANTS.md`) — this repo does not duplicate it and must not fork it.

**Any extension change touching a core-reference id** (principal/client/engagement/job/finding/
asset/object), **tenancy scoping, audit emission, or a confirm-tier verb MUST consult it first.**
This repo's code already carries inline `INV-…` tags (`INV-EXT-*`, `INV-TENANCY-05/06`,
`INV-INTEGRITY-03`, `INV-AUDIT-03/04`, `INV-SECRET-05`, …) — those are REFERENCES into lotek core's
registry, not a local one. An inline `INV-…` tag that names no real entry in lotek core's
`INVARIANTS.md` is a defect, not a stylistic choice.

The `rails_gate.py` PR gate surfaces this as a lightweight, non-blocking reminder: every
`gh pr create` gets an `additionalContext` note pointing at lotek's `INVARIANTS.md` (resolved to a
local path if one of the usual checkout locations exists on disk, else the canonical URL) — it
never fails or blocks if lotek core isn't present on this machine; it's a pointer, not a hard
cross-repo dependency. See `_g_invariant_pointer` in `.claude/hooks/rails_gate.py`.

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
