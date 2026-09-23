# feat/engagement-reverse-link — core engagement ⇄ Scribble report board

- **Status:** in review — code complete, proven by lotek's mounted suite; awaiting ext merge + core re-pin.

## Purpose

Scribble already surfaces the link from a report board back to its core scan jobs (the "Source jobs"
panel, #629). The reverse — from a **core** engagement to its **report board** — had no home: core's
engagement page couldn't reach the board, and the two nav entries ("Engagements" / "Report Boards")
read as unrelated. This adds the missing half so the relationship is navigable both directions.

## Done

- `engagement_ui.py`: new UI route `GET /engagements/by-core/<core_id>` (`engagement_by_core`).
  Resolves the board by `Engagement.core_engagement_id`; redirects to the board if one exists, else to
  the create form pre-seeded to LINK to that core engagement. Tenancy: gated on `host.can_operate_on`,
  every failure collapsed to one 404 (no existence oracle) — same rule as `_resolve_engagement`.
- `engagement_ui.py`: `engagement_new` POST honors a supplied `core_engagement_id` — points the new
  board at that existing core engagement (link) instead of `host.create_engagement` (spawn), using
  `host.can_operate_on` exactly as its docstring anticipates. No board→core duplication.
- `engagement_new.html`: carries the `core_engagement_id` through (hidden field + "linking" note);
  de-crypticised the `{{COMPANY_NAME}}` hint (it read like a template leak — it's the report placeholder).

## Remaining

- Core side (separate lotek PR): "Report board" link on `engagement_detail.html` +
  `tests/test_scribble_ui_mounted.py` cases + re-pin scribble.

## Notes

- The core-side "Report board" link is built from the mounted extension's own `url_prefix`, so it
  no-ops if Scribble isn't mounted — no hard coupling to `/scribble`.
- Proof lives in lotek's `tests/test_scribble_ui_mounted.py` (the extension's real contract is mounted),
  per this repo's "prove mounted behaviour against lotek's suite" rule; no stub-host duplicate here.
