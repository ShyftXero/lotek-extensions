# Plan: feat/cream-deliverable-engine

- **Branch:** `feat/cream-deliverable-engine`  (worktree: `.claude/worktrees/cream`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

CREAM had a correct spine (draft → issued freeze, rate-card sync, tenancy through the host seam) and
produced a document nobody would send to a client: no issuer block, no bill-to, no PDF route, no editor,
money in `float`. This branch turns it into a deliverable engine — and then into a *pentest* deliverable
engine, using the one thing a standalone invoicer can never have: the engagement's real scan data.

Five tiers, in dependency order:

1. **Party identity + PDF route + Decimal money** — CREAM produces a real document
2. **Editor UI with server-rendered live preview** — CREAM is usable without `curl`
3. **`unit`, tax/discount, execution window, quote→invoice** — CREAM is a quoting tool
4. **Scope annex + authorization-to-test block** — CREAM is a *pentest* quoting tool
5. **Burn tracking (quoted vs executed)** — CREAM is something nobody else has

## Done

- [ ] (nothing yet — plan committed first)

## Remaining

### Tier 1 — real document
- [ ] `Decimal` end to end: `models.LineItem.amount`, `service.totals`, API serialization (`float()` only
      at the JSON boundary, `quantize` for rounding). Today `Numeric(12,2)` maps to `float` and totals
      round floats.
- [ ] Issuer identity: a singleton `cream_brand` row (company name/address/email/phone/logo/accent/
      font/footer terms/default currency + tax defaults), editable in the UI, seeded on first boot.
- [ ] Bill-to **snapshot** fields on `Document` (`bill_to_name`/`_address`/`_email`) — snapshotted, not
      joined, because a client record that changes must not rewrite an issued invoice.
- [ ] Renderer: issuer block, bill-to block, meta table (number, issued/valid-until/execution window),
      totals with tax + discount, footer terms.
- [ ] `GET /documents/<id>/export.pdf` — `render_document_pdf` exists at `render.py:70` and is
      unreachable. 503 with a clear message when weasyprint is absent.

### Tier 2 — editor
- [ ] `POST /api/documents/<id>/preview` — renders **unsaved** state through the same
      `render_document_html`, returns the fragment. No second renderer in JS: a preview that can disagree
      with the PDF is worse than no preview.
- [ ] `PUT /api/documents/<id>` — full update incl. line-item replace (draft only).
- [ ] `GET /documents/<id>/edit` — form left, live preview right, ~250 ms debounce.
- [ ] `GET /documents/new` + create form (today `POST /api/documents` is the only way to make one).

### Tier 3 — quoting tool
- [ ] `LineItem.unit` (`hr`/`day`/`project`/`host`/…) + `RateItem.default_unit`. Flat vs hourly is one
      column, not a branch: flat is `qty=1, unit=project`.
- [ ] `Document.discount_pct`/`discount_amount`, `tax_label` (free text), `tax_pct`. `totals()` returns
      subtotal → discount → taxable → tax → total.
- [ ] `Document.window_start`/`window_end`; wire the already-present `valid_until` (column exists, never
      settable, never rendered).
- [ ] `POST /api/documents/<id>/convert` — quote → new invoice draft, `converted_from_id` self-ref, the
      quote left frozen. Never mutate a quote into an invoice in place.
- [ ] `POST /api/documents/<id>/mark-sent` (+ `sent_at`) — `DocStatus.sent` is currently unreachable.
      Add `DocStatus.accepted` for quotes: acceptance is what unlocks conversion.

### Tier 4 — pentest quoting tool
- [ ] Host seam `extras['engagement_scope'](engagement_id)` → target list (CIDRs/hosts/URLs/domains).
      Absent/raising → empty, like every other hook.
- [ ] `Document.scope_json` + Appendix A rendering; frozen into `snapshot_json` at issue, so the signed
      quote *is* the scope-of-record.
- [ ] Authorization-to-test block on quotes: signatory name/title/date + ROE terms, gated by
      `Document.authorization_required`.

### Tier 5 — burn
- [ ] Host seam `extras['engagement_burn'](engagement_id)` → `{unit_key: executed_hours}`.
- [ ] `GET /api/documents/<id>/burn` — quoted vs executed per line item.
- [ ] Rendered in the **editor only**, never on the client-facing document. Advisory, never auto-billed.

### Cross-cutting
- [ ] `cream/tests/` — the extension has none (`scribble/` and `vector/` both do). Pin the freeze
      invariant first; it is the rule the whole design rests on.
- [ ] Harden `_next_number` (`service.py:81`): a MAX-scan over issued numbers races two concurrent
      issues onto the sparse-unique. Per-`(kind, year)` counter row.
- [ ] `render.py` docstring still says "Fraction's report renderer" — it is `scribble` now (#4).

## Notes / gotchas

- **`create_all` already does additive migration** (`db.py:create_all`) — new NULLABLE/defaulted columns
  land on an existing database automatically. Every column this branch adds must therefore be nullable or
  defaulted, or it is silently skipped on an upgrade (the loop `continue`s on
  `not col.nullable and col.default is None`).
- **Money at the JSON boundary.** JSON has no decimal type. Internal arithmetic is `Decimal`; the API
  emits `float()` for JS consumption. Never round-trip a JSON float back into a stored amount without
  `Decimal(str(x))`.
- **Issued documents are immutable** (`service.assert_editable`). Every new mutating route goes through
  it. Convert creates a *new* document rather than reopening one.
- **CREAM is not vendored into lotek yet** — `lotek/extensions/` holds `fraction` + `vector`. Staging
  (`scripts/stage-extension.sh`) is a separate step, not part of this branch. Note lotek's vendored dir
  is still named `fraction`; upstream here is `scribble` since #4.
- Host seams added here (`engagement_scope`, `engagement_burn`) are **optional and fail-safe**, matching
  `deps.py`: absent → standalone default, raising → safe default, never a 500.
