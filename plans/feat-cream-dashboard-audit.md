# Plan: feat/cream-dashboard-audit

- **Branch:** `feat/cream-dashboard-audit` (off `main`)
- **Status:** 🟡 code + tests green locally — not yet PR'd
- **Issues:** extension UI/UX-maturity sweep (cream half)

## Purpose

cream is already the most UI-complete extension; two gaps from the maturity survey:

1. **Unbounded dashboard, no way to find a document.** `dashboard()` iterated *every* visible `Document`
   into one table with no cap, filter, or search — a firm with a few hundred invoices got an unbounded
   page. Now: the read-scope + `status`/`kind` filters run in SQL (both columns are indexed), the list
   is capped at `_DASHBOARD_MAX = 200` and announces truncation instead of dropping rows silently, and
   the page carries a status/kind filter form (`onchange` auto-submit, `<noscript>` fallback, Clear).
   Filters are enum-validated in the route, so a raw query-arg value never reaches the page as free text.
2. **cream's audit verbs weren't declared to the host.** cream emits `issue`/`mark_sent`/`accept`/
   `convert`/`void`/`update_brand` via `host_audit(...)`, but the manifest had no `[audit]` block, so
   the host's `/admin/audit` filter couldn't select any of them (INV-AUDIT-03's reader half). Added the
   `[audit] verbs` block + a drift-guard test that greps the real call sites.

## Done

- `blueprint.py`: `_DASHBOARD_MAX` + `_as_doc_enum` fail-open parser; `dashboard()` builds a SQL
  statement (visibility `IN`, status/kind `WHERE`, `LIMIT max+1`), renders ≤max rows, passes
  `truncated`/filter/enum context.
- `templates/cream/list.html`: status+kind filter form; filter-aware empty state; truncation notice.
- `templates/cream/_ui.html`: `.cr-filters` / `.cr-trunc` styles (cream's own palette — cream is a
  self-consistent design system, not reskinned here).
- `lotek-extension.toml`: `[audit] verbs = [issue, mark_sent, accept, convert, void, update_brand]`.
- Tests: `test_dashboard_filter.py` (status filter, kind filter, unknown-filter ignored, cap+truncation
  via a monkeypatched `_DASHBOARD_MAX`) + a manifest verb drift-guard in `test_audit.py`. Full cream
  suite green (182); ruff + pyrefly clean.

## Remaining

- `/security-review` + `/adversarial-reviewer`; PR.
- (Not done, out of scope) cross-page `url_for` hardening (Acid_Burn's small nit); client-name search.

## Notes / gotchas

- Pushing the visibility filter into SQL (was a post-query Python `continue`) is what makes the row cap
  correct — a SQL `LIMIT` applied before visibility filtering would under-fill the page.
- `_as_doc_enum` returns `None` for a blank/unknown value → no filter, never a 500; it is also the thing
  that keeps the query-arg off the page (only a real enum `.value` is echoed into the `<select>`).

## Evals

- **Hypothesis:** the dashboard filters by status and kind, is bounded + announces the cap, and cream's
  audit verbs become host-selectable — with the existing rows/handles unchanged. **Mode:** 2.
- **Graders:** cream's own suite; the 5 new tests.
- **Baseline:** before, `dashboard()` had `select(Document)...all()` with a Python visibility skip and
  no cap; manifest had no `[audit]` block.
