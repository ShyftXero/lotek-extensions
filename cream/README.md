# CREAM

Client **quoting + invoicing** for lotek — a mountable Flask extension. Generates SOW/quotes and
invoices; **no payment processing** (collection happens off-platform). Mounts into lotek under `/cream`,
or runs standalone.

**Operator documentation: [`docs/CREAM.md`](https://github.com/ShyftXero/lotek-extensions/blob/main/cream/docs/CREAM.md)** —
UI surfaces, data model, the PAT machine API, the deliverable, and the security posture. It ships inside
the wheel and renders on lotek's in-app Docs page.

## How lotek consumes this

As a **pinned git dependency**, not a vendored copy. lotek declares `cream` in its `pyproject.toml` with
a `[tool.uv.sources]` entry pointing at this repo's `cream/` subdirectory, and discovers the installed
package through the `lotek.extensions` entry point:

```toml
[project.entry-points."lotek.extensions"]
cream = "cream"
```

Mount metadata (url prefix, nav, seed, machine prefix, owned tables, docs) travels inside the wheel as
`cream/lotek-extension.toml`, so an installed package is self-describing. Bump lotek's pin with
`uv lock --upgrade-package cream`. There is no vendoring step and no staging script.

## Model (v2-native)

- **UUIDv7 PKs** everywhere. Cross-core references (`engagement_id`, `client_id`, `owner_id`,
  `document_object_id`) are **UUID soft references** — no cross-schema FK, never Integer.
- **No authorization data.** Tenancy is the host's; `engagement_id` records which engagement a document
  bills, and the host seam decides who may read it.
- Tables: `cream_brand`, `cream_rate_cards`, `cream_rate_items`, `cream_documents`, `cream_line_items`,
  `cream_number_counters`.
- Money is `Decimal` throughout, never `float`.

## Lifecycle

- A **draft** is fully editable; the rate card only ever *suggests* line items.
- **Issuing** freezes an immutable, numbered snapshot (`INV-YYYY-NNNN` / `Q-YYYY-NNNN`); the document is
  read-only thereafter and renders from that snapshot. Issued documents are **voided, never deleted**.
- Issue / void / mark-sent / accept / convert are **human-only** — they are not on the PAT machine API.

## Mount contract

```python
cream.register(app, engine, *, url_prefix="/cream", instance_path=None,
               base_template="cream/base.html", client_model=None,
               session_factory=None, create_tables=True, **host_models) -> CreamConfig
```

Host capabilities arrive via `config.extras` (PAT hooks, `current_actor`, `can_write`,
`can_operate_on`, `visible_engagement_ids`, …).

## Development

```sh
uv sync --extra dev
uv run pytest
uv run ruff check cream tests
```

PDF export needs the optional extra: `pip install 'cream[pdf]'` (weasyprint).
