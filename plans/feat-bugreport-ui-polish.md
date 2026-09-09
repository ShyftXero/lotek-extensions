# Plan: feat/bugreport-ui-polish

- **Branch:** `feat/bugreport-ui-polish` (off `main`)
- **Status:** 🟡 code + tests green locally — not yet PR'd
- **Issues:** extension UI/UX-maturity sweep (bugreport half)

## Purpose

bugreport was feature-complete but failed the exploiteer maturity bar on presentation and safety. Four
fixes, all in the one `list.html` page + its blueprint:

1. **Theme consistency.** `list.html` shipped a parallel `.br-*` design system with hardcoded dark hex,
   so under the host's light / h4xor themes it stayed dark. Reskinned onto the host CSS variables
   (`--card`/`--ink`/`--line`/`--accent`/`--danger`/`--field-bg`…). `base.html`'s standalone appbar keeps
   its own hex on purpose — standalone has no host vars to inherit.
2. **Feedback (PRG banners).** Validation failures rendered Flask's generic 400 page and successes gave
   no confirmation. Now: `Invalid` → redirect back with `?error=` (inline `br-banner-err`), success →
   `?notice=` (`br-banner-ok`). **Authorization refusals (`Denied`) and the write gate stay hard 403** —
   an authz refusal is not a reflected banner, preserving the no-existence-oracle posture.
3. **Confirm on destructive actions.** `confirm()` added to delete-report, delete-file, stop-sharing and
   rotate-link (a misclick was unrecoverable / silently revoked a live capability).
4. **Announced truncation.** The list is capped at `LIST_LIMIT`; the page now says so and points at the
   machine API instead of silently dropping rows.

## Done

- `blueprint.py`: `_back(notice=/error=)` PRG helper; `bugreport_list_limit` in context;
  all 8 write handlers redirect-with-banner on success / `Invalid`, keep `Denied`→403, `_blobs_or_503`→503.
- `list.html`: host-var reskin; top-of-page notice/error banners (autoescaped); 4 `confirm()`s;
  per-list truncation notices.
- Tests: `test_ui.py` — 2 renamed (Invalid now 302+banner) + 4 new (notice banner, host-var theming,
  delete confirm, truncation). `test_attachments.py` — 2 oversize/too-many uploads now 302+banner.
  Full bugreport suite green; ruff + pyrefly clean.

## Remaining

- **BusyBody persona** (Null_Pointer's item 3) — a lotek CORE-repo file
  (`tests/busybody/corpus/bugreport-browser.yaml` + a lotek-side test). Tracked separately; NOT in this
  extension PR.
- PR.

## Review (adversarial, on the committed diff)

No blockers. Verified: no XSS (autoescaped), no open redirect (`_back` targets a fixed internal
endpoint), authz preserved (every `Denied`/write→403, `_load_or_404`→404, `_blobs`→503 unchanged; only
input-`Invalid` became a banner), no partial writes on the error path, CSRF unchanged. Resolved:
- **CONCERN (fixed here):** the banner rendered `request.args.get('notice'|'error')` verbatim, so a
  crafted `?error=<text>` link reflected attacker-chosen plain text into the trusted banner chrome (a
  phishing aid — not XSS). Now `notice`/`error` are short CODES mapped to fixed messages server-side in
  the template; an unknown code renders nothing. `test_a_crafted_banner_code_renders_no_banner` pins it.
- **NIT (fixed here):** the delete-confirm test asserted any `confirm(` on the page; now it pins the
  delete-report form's own message. Two banner tests tightened from the always-present `.br-banner-*` CSS
  class to the rendered `<p class="br-banner ...">` element.

## Notes / gotchas

- Reflected banners render `request.args.get('notice'|'error')` through Jinja autoescaping — the text is
  a fixed success string or a service-authored validation message, never raw user HTML.
- Session CM is a plain `sessionmaker`; each service fn commits itself only after its `raise Invalid`
  validation, and `Session.__exit__` rolls back when no commit ran — so catching `Invalid` and returning
  a redirect commits nothing.

## Evals

- **Hypothesis:** the page follows the host theme, every write gives inline feedback, destructive
  actions are confirmed, and truncation is announced — with authz semantics unchanged. **Mode:** 2
  (behaviour change on the error path).
- **Graders:** bugreport's own suite; the 2 updated + 4 new UI tests + 2 attachment tests.
- **Baseline:** before, `list.html` carried `#0e151c`/`#37c9d6`… and every handler `abort(4xx)`/silent 302.
