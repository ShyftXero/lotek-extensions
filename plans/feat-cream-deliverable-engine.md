# Plan: feat/cream-deliverable-engine

- **Branch:** `feat/cream-deliverable-engine`  (worktree: `.claude/worktrees/cream`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge — all five tiers landed, 111 tests green

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

All five tiers. 111 tests (`cream/tests/`, where the extension had none), `ruff` clean, and the suite is
green both with and without the optional `weasyprint` dependency.

### Tier 1 — real document
- [x] `Decimal` end to end — new `cream/money.py` (coerce through `str`, quantize half-up, `float()`
      only at the JSON edge). `Numeric(12,2)` columns are `Mapped[Decimal]`; `LineItem.amount` no longer
      multiplies floats.
- [x] `cream_brand` singleton: name/address/contact/tax id/logo/accent/font/currency + tax defaults,
      payment instructions, footer terms, default ROE. Editable at `/cream/brand`, seeded on first boot.
- [x] Bill-to **snapshot** fields on `Document` (+`bill_to_attn`).
- [x] Renderer rewritten: issuer block, bill-to, meta table, unit-aware line table, discount/tax totals,
      payment + footer blocks.
- [x] `GET /documents/<id>/export.pdf` — verified to return real bytes (`%PDF-`, 19 379 B); an honest
      503 naming `weasyprint` when the optional dep is absent, never HTML under a `.pdf` filename.

### Tier 2 — editor
- [x] `POST /api/documents/<id>/preview` — unsaved state applied inside a savepoint that is **always
      rolled back**, then rendered by the same `build_view` + `render_document_html` a save would use.
- [x] `PUT /api/documents/<id>` — whole-draft save incl. line-item replace, whitelisted fields only.
- [x] `GET /documents/<id>/edit` — form + live preview, 250 ms debounce, stale-response guard.
- [x] `GET /documents/new`.

### Tier 3 — quoting tool
- [x] `LineItem.unit` + `RateItem.default_unit` — flat vs hourly is `(1, project)` vs `(16, hr)`.
- [x] Discount (pct or fixed, clamped to the subtotal) + free-text `tax_label` and `tax_pct`;
      `totals()` returns subtotal → discount → taxable → tax → total.
- [x] `window_start`/`window_end`/`due_date`; `valid_until` now settable and rendered.
- [x] `POST /convert` — quote copied into a NEW invoice draft, `converted_from_id`, quote untouched.
- [x] `POST /mark-sent`, `POST /accept`, `DocStatus.accepted`.

### Tier 4 — pentest quoting tool
- [x] `extras['engagement_scope']` → Appendix A, frozen into the snapshot at issue.
- [x] Opt-in authorization-to-test block: ROE terms + signature line + signatory name/title.
- [x] `extras['engagement_units']` — added because `sync` was previously uncallable from a browser
      (it required the caller to already know the engagement's unit keys).

### Tier 5 — burn
- [x] `extras['engagement_burn']` + `GET /api/documents/<id>/burn`; editor-only, and an unmeasured line
      reads "not measured" rather than zero.

### Cross-cutting
- [x] Issued documents **render from their snapshot** (`viewmodel.view_for`) — the fix that makes
      "client copy == your copy" true rather than merely intended.
- [x] `_next_number` uses a locked per-`(kind, year)` counter, seeded from existing numbers so a
      database numbered by the old MAX-scan continues its sequence.
- [x] `cream/markup.py` — escape-first restricted markup for scoping prose.
- [x] `render.py` docstring now says Scribble, and `lotek-extension.toml` gained the branding nav entry.

### Security review of this diff — two findings, both fixed here
- [x] `render.safe_logo` accepted any `data:image/*` while `api._clean_logo` refused SVG: the inner half
      of a claimed defence-in-depth pair was the looser one. Both now share a raster allowlist.
- [x] `PUT /api/brand` was gated on write capability alone, but branding carries `payment_instructions`
      — the remit-to block on every future document. Any writer could have re-routed remittance with no
      per-document trace. **Admin-only** now (`current_actor_is_admin`), read still open.

### Red-then-green transcripts (8 guards, each broken and watched fail)

| Guard | broken | repaired |
|---|---|---|
| issued doc renders from its snapshot | 1 failed | 1 passed |
| logo SSRF: remote URL dropped | 1 failed | 1 passed |
| preview never persists | 1 failed | 1 passed |
| money is Decimal, not float | 1 failed | 1 passed |
| tax applies after discount | 1 failed | 1 passed |
| branding is admin-only | 1 failed | 1 passed |
| escape-first markup | 6 failed | 6 passed |
| frozen docs refuse all mutation | 4 failed | 4 passed |

The Decimal row is the one worth reading. The **first** version of that guard stayed GREEN against a
deliberate float-multiply regression: it used 16 × 249.99, which quantizes to the same cent either way,
so it was pinning nothing. It was re-armed with 2.5 hr × $150.07 — exactly 375.175, half-up to 375.18,
where the float path gives 375.17 — and now goes red. A passing test is a hypothesis until you watch it
fail.

## Remaining

Nothing blocking. Deliberately out of scope:

- **Vendoring into lotek.** `scripts/stage-extension.sh` is a separate step and needs the host to supply
  the four seams (`engagement_scope`, `engagement_units`, `engagement_burn`, plus the existing tenancy
  hooks). Until then those degrade to empty and CREAM behaves as a standalone quoting tool.
- **A `pyrefly` pass.** Not configured for this repo's extensions; `ruff` is the lint gate here.
- **Browser-driven e2e.** The editor is covered at the route/contract level, not by a real browser.

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
