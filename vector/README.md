# Vector — attack-path visualization

Author interactive kill-chain / attack-path diagrams **entirely in the browser**, persist them, import
and export the JSON model, and export a **self-contained interactive HTML** file as the deliverable (the
viewer runtime + model JSON inlined into one offline file).

**Operator documentation: [docs/VECTOR.md](docs/VECTOR.md)** — UI surfaces, the `vector.attackpath/v1`
model, owned tables, the machine (PAT) API, the deliverable, and the security posture.

## How it's consumed

lotek consumes Vector as a **pinned git dependency**, discovered through the `lotek.extensions` entry
point (`vector = "vector"` in `pyproject.toml`). It is **not vendored** and **not** installed by any
`stage-extension.sh` script (that path is retired). A mount manifest (`lotek-extension.toml`, shipped
inside the wheel) tells the host how to mount it: `/vector` prefix, one nav entry, the `/machine` PAT
surface, `vector_`-prefixed owned tables, and the seed callable. Bump it in lotek with
`uv lock --upgrade-package vector`.

## Two ways to run, one codebase

- **Mounted into a host** (lotek) via the `register()` contract — the host injects its engine, session
  factory, base template, and capability hooks (current-actor / can-write / PAT). Vector never imports
  host internals; everything host-specific arrives through the injected config. Tables are
  `vector_`-prefixed so they never collide in the shared database.
- **Standalone** — a minimal Flask app on its own SQLite DB, for offline authoring:
  ```sh
  uv sync --extra dev
  python -m vector            # serves http://127.0.0.1:5099/vector
  ```

The **viewer runtime** (`vector/static/vector-viewer.{js,css}`) renders the model for both the in-editor
live preview and the exported deliverable, so the deliverable can never drift from the preview.

## Provenance

Internal tooling. The bundled read-only example (`Spark Range — Red Team Attack Path`) is a fictional
range scenario used to validate the model.
