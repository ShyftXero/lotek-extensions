# CREAM

Client **quoting + invoicing** for lotek — a mountable Flask extension. Generates SOW/quotes and
invoices; **no payment processing** (collection happens off-platform: Square/PayPal). Mounts into lotek
under `/cream`, or runs standalone.

## Model (v2-native)

- **UUIDv7 PKs** everywhere (`cream.db.UuidPk`). Cross-core references (`engagement_id`, `client_id`,
  `owner_id`, `document_object_id`) are **UUID soft references** — no cross-schema FK, never Integer.
- **No authorization data.** Tenancy is core's; `engagement_id` merely records which engagement a
  document bills.
- Tables `cream_*`: `cream_rate_cards`, `cream_rate_items`, `cream_documents`, `cream_line_items`.

## Lifecycle

- A **draft** tracks its engagement live: `POST /cream/api/documents/<id>/sync` returns SUGGESTED line
  items (from an editable rate card) for billable units not yet on the doc — a human accepts/edits.
- **Issuing** (`/issue`) freezes an immutable, numbered snapshot (`INV-YYYY-NNNN` / `Q-YYYY-NNNN`);
  the doc is read-only thereafter (`DocumentFrozen`). Issued docs are **voided, never deleted**.
- Rendering is CREAM's **own lightweight** HTML pipeline (`cream.render`); PDF is optional
  (`pip install .[pdf]` → weasyprint).

## Mount contract

`cream.register(app, engine, *, url_prefix, base_template, client_model, session_factory, create_tables)`
→ `CreamConfig`. Host capabilities arrive via `config.extras` (`current_actor`, `can_write`).

Staged into lotek via `scripts/stage-extension.sh <this-repo> cream cream /cream cream.seed:seed_defaults`.
