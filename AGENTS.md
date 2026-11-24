# AGENTS.md: lotek-extensions repo rails

This file holds repo-specific rails only: git, dev, test, deploy, and review lenses. The global cross-repo rails live in [lotek-cadre/AGENTS.md](https://github.com/ShyftXero/lotek-cadre/blob/main/AGENTS.md). Read that file first.

## Roster

Route work per the global roster. The front man is zero-cool. This repo mostly uses acid-burn (seams), cereal-killer (tests), joey (catch-all), the-plague (review), and margo (security). Do not copy the roster table here.

## Layout

This is one monorepo of independent `uv` subprojects. `kit/` is not an extension. Each extension owns its own venv, lock, and `pyproject.toml`.

## Git

- Base every branch on `origin/main`. Fetch first, then rebase before commit.
- Stage explicit paths. The rails gate blocks `git add -A` and `git add .`.
- Stacked PRs: merge in order. A wrong merge order closes a PR.
- Merge with the repo method. Do not squash.

## Tests and lint

Run these from the subproject directory that you touched:

```sh
uvx ruff check <ext>
cd <ext> && uv run --extra dev pyrefly check <files>
cd <ext> && uv run --extra dev pytest -q
```

Prove mounted behaviour against lotek too: `lotek/tests/test_<ext>_extension.py`.

## Run

There is no standalone service. lotek loads each extension through the `lotek.extensions` entry point. To test an extension live, stand up a lotek ephemeral instance with an extension override:

```sh
scripts/ephemeral-instance.sh up --ext <name>=<branch>
```

## Deploy and ship back

Extensions ship as pinned git dependencies. A tag here is not a deploy.

1. Merge the subdir into `main`.
2. The release workflow cuts the dated tag. Do not cut it by hand while Actions is active.
3. Re-pin in lotek: update `[tool.uv.sources]` in `pyproject.toml`, run `uv lock --upgrade-package <ext>` and `uv sync --extra extensions`, run the mounted tests, then PR into lotek.

## Review lenses

- the-plague: adversarial review. The PR needs a `### PLAGUE REVIEW` comment ending `VERDICT: ship @ <sha>` at HEAD.
- margo: `/security-review` on `git diff main...HEAD`.
- The PR gate is a gate. Record every marker before `gh pr create`:

```sh
/security-review
python3 .claude/hooks/rails_gate.py --ack-review
/adversarial-reviewer
python3 .claude/hooks/rails_gate.py --ack-adversarial
python3 .claude/hooks/rails_gate.py --ack-tests
python3 .claude/hooks/rails_gate.py --ack-transcripts
```

## Writing

Use ASD-STE100. Never use an em dash. See the global doc.
