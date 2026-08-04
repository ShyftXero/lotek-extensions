# Vector — attack-path visualization

Author interactive kill-chain / attack-path diagrams **entirely in the browser**, persist them, import
and export the JSON model, and export a **self-contained interactive HTML** file as the deliverable.

Vector runs two ways from one codebase (the Fraction pattern):

- **Standalone** — a minimal Flask app with its own SQLite database:
  ```sh
  uv sync --extra dev
  python -m vector            # serves http://127.0.0.1:5099
  ```
- **Mounted into a host** (e.g. lotek) via the `register()` contract — the host injects its engine,
  session factory, base template, and capability hooks; Vector shares the host DB with `vector_`-prefixed
  tables and renders inside the host's shell. Vector never imports host internals; everything
  host-specific arrives through the injected config (`cfg.extras['host']`).

## The model — `vector.attackpath/v1`

A diagram is one JSON document:

- **zones** — trust zones laid out left→right (columns).
- **boundaries** — optional firewall/segmentation markers between zones.
- **nodes** — hosts/assets placed in a zone + row, each carrying a **state timeline** (`states[]`: at
  which phase the node becomes a target / owned / a beacon / impacted …, with optional status labels).
- **edges** — attacker actions between nodes (kind, phase, route, label).
- **phases** — the ordered walkthrough steps: title, MITRE tactics/technique, description, targets, a
  "what to watch on the map" note, and an optional Blue-team detection block (tool, finding, query,
  what-was-seen, caveat/gap).
- **style** — optional catalogs (edge kinds, node states, roles, tactic kinds); defaults reproduce the
  built-in cyber-dark theme, so a minimal diagram renders without any style block.

The **viewer runtime** (`vector/static/vector-viewer.{js,css}`) renders this model — it powers both the
in-editor live preview and the exported deliverable, so the deliverable can never drift from the preview.

## License / provenance

Internal tooling. The bundled example diagram is a fictional range scenario used to validate the model.
