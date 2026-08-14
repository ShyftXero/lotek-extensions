# Scribble

A pentest **vulnerability-template library + reporting engine** — a Python re-imagining of OWASP
FACTION's write-up library and report generation. It mounts into lotek at `/scribble` and turns scan
output and hand-authored findings into a client deliverable (self-contained HTML, an HTML+artifacts zip,
or an editable `.docx`).

- **Vuln template library** — reusable write-ups with `{{VARIABLE}}` placeholders; 63 seeded on first boot.
- **Report boards** — per-engagement, two-level drag tree (assessment-type groups → findings); board
  order is document order.
- **Finding editor** — per-block rich text with autosave, presence, and inline image paste; artifacts
  (screenshots/text/files) per finding with caption, include/exclude and ordering.
- **Machine API** — nine PAT/Bearer routes under `/scribble/machine` so an agent can create engagements,
  promote a whole scan job, upload evidence and drive the vuln map.
- **Two deliverables from one context** — HTML (print-to-PDF) and `.docx`.

## Operator documentation

**[docs/SCRIBBLE.md](https://github.com/ShyftXero/lotek-extensions/blob/main/scribble/docs/SCRIBBLE.md)**
— UI surfaces, data model, the full machine API table, report output, and the security posture. It
ships inside the wheel and renders on lotek's in-app Docs page.

## How lotek consumes this

As a **pinned git dependency**, resolved by `uv` and discovered through the `lotek.extensions`
entry-point group. There is no vendoring and no staging script — lotek installs the package, reads its
mount metadata (`url_prefix`, nav entries, machine prefix, owned tables, seed callable) from the
`lotek-extension.toml` shipped inside the wheel, and mounts it when the extension is enabled.

Bump it from the lotek side:

```sh
uv lock --upgrade-package scribble
```

## Development

```sh
uv sync --extra dev
uv run pytest          # unit + smoke tests
uvx ruff check .       # lint
```

Rails mirror lotek's: short-lived branches off `main` (`feat/`·`fix/`·`chore/`), one logical change per
commit, explicit staging, `ruff` + `pyrefly` clean, tests with every change, adversarial review before
merge.
