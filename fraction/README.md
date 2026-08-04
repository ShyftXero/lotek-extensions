# Fraction

A pentest **vulnerability database + reporting engine**. A Python re-imagining of OWASP FACTION's
vulnerability-template library and report generation, built to [Lotek](https://github.com/)'s
conventions and designed to **mount into Lotek** (`fraction.register(app, engine, ...)`) while also
running **standalone** for solo development.

- **Vulnerability template library** — reusable finding write-ups with Jinja `{{VARIABLE}}` placeholders
  (`{{COMPANY_NAME}}`, `{{TARGET_HOST}}:{{TARGET_PORT}}`, `{{TARGET_URL}}`, …).
- **Engagements** — pick a template, add it to an engagement as an editable finding, attach artifacts.
- **Finding board** — two-level drag-and-drop tree: assessment-type groups → findings; board order is
  document order.
- **Artifacts** — screenshots/text/files per finding with per-artifact include/exclude, caption, order.
- **Rich text** — TipTap editor with inline image paste; autosave + presence now, CRDT co-editing later.
- **Two deliverables from one context** — a self-contained HTML report (print-to-PDF) and an editable
  `.docx` (docxtpl template converted from FACTION's default).

See **[PLAN.md](PLAN.md)** for the full architecture and the parallel build plan, and
**[plans/CONTRACTS.md](plans/CONTRACTS.md)** for the frozen interfaces every workstream builds against.

## Quick start (standalone)

```sh
uv sync --extra dev
uv run python standalone_app.py       # boots the themed shell at http://127.0.0.1:5057/fraction/
uv run pytest                          # unit + smoke tests
uvx ruff check .                       # lint
```

## Status

Sprint 0 (scaffold + data model + contracts). Feature workstreams are built in parallel per PLAN.md §13.

## Dev workflow

Mirrors Lotek's rails: short-lived branches off `main` (`feat/`·`fix/`·`chore/`), one logical change per
commit, explicit staging (never `git add -A`), `ruff` + `pyrefly` clean, tests with every change (that
assert real state, not proxies), an adversarial-review gate before merge, and lead-driven merges. Each
in-flight branch carries a `plans/<branch-slug>.md` (copy `plans/TEMPLATE.md`). Full rules:
**[docs/RAILS.md](docs/RAILS.md)** (and [CLAUDE.md](CLAUDE.md) for the summary).
