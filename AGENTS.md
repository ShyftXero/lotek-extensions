# lotek-extensions repo rails

This repo inherits the global `AGENTS.md` (`~/.pi/agent/AGENTS.md`). This file lists repo-specific rails only.

Model pinning policy: pin Claude models to the 4.x series only. Do not pin or reference any 5.x models.

## Canonical sources

- Ship loop: `ShyftXero/lotek-cadre` `cell/SHIP_LOOP.md`.
- Merge gate and rulesets: `ShyftXero/lotek-cadre` `cell/rulesets/README.md`.
- Owned repos: `ShyftXero/lotek-cadre` `cell/owned-repos.txt`.
- Security contract: `ShyftXero/lotek` `INVARIANTS.md`.

Do not copy these files into this repo. Link to them.

## Inherited rules this repo depends on

- Dispatch: one shot spawns at depth 1. No fan-out. Fail fast on a dead tool or a dead path. No cross-session messaging.
- Roster: use only the persona names in the global `AGENTS.md`. Do not invent a name.
- Tools: every persona has `read`, `write`, `edit`, `bash`, `grep`, `find`, `ls`, and the HUD tools. The front man is not denied tools.
- Models: pin an explicit 4.x model. Never pin a 5-series model.
- Browser work: run it on an allowed hhr node. The denied list is `lotek-cadre/guards/pi/denied-hosts.json`. The guard is `lotek-cadre/guards/pi/browser-host-guard.ts`.
- Merge approval: a `VERDICT: ship @ <sha>` verdict on the current HEAD is approval.
- HHR nodes: ephemeral substrate. Stage missing tools yourself. Run heavy tests on an hhr node, not a dev box.

## Layout

Each extension lives in its own subdirectory. The extensions are `bugreport/`, `cream/`, `exploiteer/`, `pastel/`, `registrar/`, `scribble/`, `sshim/`, and `vector/`.

Each extension carries its own `pyproject.toml`, `lotek-extension.toml`, package, tests, and lock file. A change touches one extension unless it is a cross-cutting repo change.

An extension reaches lotek only through the injected host contract. Do not import lotek or another extension.

`kit/` holds `lotek-kit`. It is a shared contract library, not an extension. It has no `lotek-extension.toml` and no `register()`.

Admit code to the kit only when two consumers that may not import each other both need it. Convenience is not a reason. The kit has no runtime dependencies. The kit never imports lotek or an extension. `kit/tests/` pins these rules.

## Worktrees and branches

Use one worktree per session. Use `wt switch --create <type>/<name>`. It creates a sibling directory.

Do not create worktrees under `.claude/worktrees/` or any hidden subdirectory. The pi `worktree-nudge` extension blocks those paths.

Base every branch on `origin/main`. Run `git fetch origin` first.

Use one of these prefixes: `feat/`, `fix/`, `chore/`, `docs/`. Put the extension name in the branch name when it helps. Example: `feat/cream-pdf`.

## Commit gate

Stage explicit paths. Run:

```sh
git add <paths>
git commit -m "type: message"
```

The gate blocks `git add -A`, `git commit -a`, and a direct commit to `main`.

`ruff` must pass on the staged Python. `pyrefly` must pass for each touched subproject:

```sh
uvx ruff check <ext>
cd <ext> && uv run --extra dev pyrefly check <files>
```

Override one command with `RAILS_OVERRIDE=1 <cmd>`. Every override is logged to `<git-dir>/claude-rails-audit.jsonl`.

## Review markers

`gh pr create` denies while an applicable marker is missing for the current HEAD. This repo has no release step of its own, so the PR is the only gate for the extension tests.

```sh
/security-review
python3 .claude/hooks/rails_gate.py --ack-review
/adversarial-reviewer
python3 .claude/hooks/rails_gate.py --ack-adversarial
cd <ext> && uv run --extra dev pytest -q
python3 .claude/hooks/rails_gate.py --ack-tests
python3 .claude/hooks/rails_gate.py --ack-transcripts
```

Run `--ack-transcripts` when the branch changes a `tests/` directory.

A marker binds to `git rev-parse HEAD`. A new commit voids it. Re-run and re-ack.

A branch whose changed paths are all `.md` skips `--ack-tests`. It still needs both reviews.

`--ack-invariants` is a pointer here, not a gate. The invariant contract and its tests live in lotek core. An `INV-...` tag must name a real entry in that file.

## Merge gate

A branch ruleset requires the `review/lotek-plague` status from the reviewer App. See `cell/rulesets/README.md`.

Merge with a merge commit:

```sh
gh pr merge <n> --merge --match-head-commit <sha>
```

The ruleset allows merge commits only. Squash and rebase are refused. Do not force-push.

Use the persona GitHub App token for a GitHub write. Do not use human auth.

## Stacked PRs

Merge the upper PR into the lower PR before you delete the lower base branch. GitHub closes a PR when its base branch is deleted. It does not retarget the PR.

To recover, restore the base ref, reopen the PR, retarget it to `main`, then merge it.

Review each PR against its parent branch: `git diff <parent-branch>...HEAD`. Do not review the whole stack against `main`.

## Tests

Every change ships with tests that pin the behavior. A bugfix adds a test that fails before the fix and passes after it.

Run the touched extension's tests in its own subproject:

```sh
cd <ext> && uv run --extra dev pytest -q
```

Prove a mounted behavior in lotek core too. An extension's real contract is how it behaves inside lotek. The mounted test lives in `lotek/tests/test_<ext>_extension.py`.

Browser host guard: when you run mounted tests in lotek, heavy UI and e2e tiers are denied on forbidden hosts by lotek's no-suite-here gate. Run them on hhr hosts. See lotek `AGENTS.md`, "Off-limits hosts". The guard is `lotek-cadre/guards/pi/browser-host-guard.ts`. The denied list is `lotek-cadre/guards/pi/denied-hosts.json`.

## Extension contract

- Use a UUIDv7 surrogate primary key.
- Store a core reference as `sqlalchemy.Uuid`, or `SoftHostId` for a text-backed compatibility column. Never use `Integer` or `String`.
- Keep authorization data out of the extension. Resolve engagement rights through the host seam: `can_operate_on` and `visible_engagement_ids`.
- Prefix the extension's own tables.
- Stage and audit a confirm-tier outward action on the server.

### Authorization must pass when mounted

Every host predicate reads `g.principal` and fails closed when it is absent. A stub host proves your logic. It does not prove the mount.

Add a mounted test for any authorization path. Assert the allowed case as well as the refused case.

### Core reference column type

A core id column declared as `Integer` or `String` fails on Postgres with `cannot cast type uuid to integer`. SQLite accepts the value, so a SQLite run proves nothing.

Use `SoftHostId` for a core reference. There is no `ForeignKey` to a core table, because the table is unknown until mount time.

Run this check before you ship:

```sh
grep -rnE "(_id|_by)\s*:.*mapped_column\((Integer|String)" <ext>/*/models.py
```

`create_all` does not change the type of an existing column. Recreate or migrate the table.

A non-editable `uv` path install is cached by version. Force a rebuild with `uv sync --reinstall-package <ext>`.

### Local checkout for development

Use a non-editable path source in lotek's `[tool.uv.sources]`:

```toml
scribble = { path = "../lotek-extensions/scribble" }
```

Do not set `editable = true`. An editable install skips the wheel build, so `lotek-extension.toml` is absent and discovery silently skips the extension.

Revert the override and sync the released tag before the final gate run.

## Shipping to lotek

Extensions are pinned git dependencies. lotek installs each one from this monorepo by tag. Vendoring is retired.

1. Merge the extension change here. `.github/workflows/release-tag.yml` cuts a dated tag `v<YYYY.M.D.HHMMSS+g<sha>>` on merge. Do not cut the tag by hand.
2. Re-pin the extension in lotek. Update `tag = "..."` under `[tool.uv.sources]` in lotek's `pyproject.toml`. Run `uv lock --upgrade-package <ext>` and `uv sync --extra extensions`. Run the mounted tests.

A tag here is not a deploy. lotek deploys the extension only after lotek re-pins it and cuts a core release.

Do not edit an installed copy under lotek's environment. The next `uv sync` replaces it.
