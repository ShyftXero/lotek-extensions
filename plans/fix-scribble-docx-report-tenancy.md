# Plan: fix/scribble-docx-report-tenancy

- **Branch:** `fix/scribble-docx-report-tenancy`  (worktree: `.claude/worktrees/docx-authz`, off `main`)
- **PR:** opening now
- **Status:** 🟢 done, ready for merge

## Purpose

`GET /engagements/<id>/report.docx` (`scribble/scribble/report_docx_api.py`) builds and streams the
report with **no tenancy check at all** — unlike its sibling `/report` HTML route
(`report_html_api.py`), it never calls `_authorize_engagement_view`. Any authenticated actor who can
guess or enumerate an `engagement_id` can download **another client's** findings/evidence as a
`.docx`. Found while re-vendoring Scribble onto lotek v2 (`lotek#scribble/v2-align`) during that
branch's mandatory adversarial review, then traced back here: confirmed present on this repo's
`origin/main` tip (`c07f752`) too — it predates that PR and is not specific to lotek v2.

This closes the same gap `#6` (`fix(scribble): the stub host must provide can_view_client`) closed for
the HTML route, applied to the DOCX route, which `#6` never touched.

## Done

- [x] Import and call `report_html_api._authorize_engagement_view` from `report_docx_api.py` — the
      same host-delegated `can_view_client` check the HTML route already uses. No circular import:
      `report_docx_api` importing from `report_html_api` at module scope resolves cleanly (verified
      via `python -c "import scribble.report_docx_api"`); neither module is imported by the other, only
      by `scribble/__init__.py`.
- [x] Regression test `tests/test_report_docx_authz.py` mounting the real `stub_host` fixture (mirrors
      `tests/test_scribble_report_authz.py`'s pattern), covering DENY (no grant, wrong-client grant,
      NULL-client non-admin, host missing `can_view_client`), ALLOW (client-grant, admin bypass), and
      the standalone-no-host case. 7 tests.
- [x] Red→green proven: with the `_authorize_engagement_view(engagement)` call commented out, the 4
      DENY-case tests fail (`assert 200 == 404`); restoring the call turns them green.
- [x] `uv run ruff check scribble/report_docx_api.py tests/test_report_docx_authz.py` clean.
- [x] `uv run pyrefly check scribble/report_docx_api.py` — 0 errors.
- [x] Full suite: `uv run pytest -q` — 487 collected, 9 failed / 478 passed. All 9 failures are in
      `tests/test_skill.py` (a `skill/scribble-report-refine/` directory + `SKILL.md` that this test
      file expects but which has never existed anywhere in this repo's git history — pre-existing on
      `main` at `c07f752`, unrelated to this change). All 30 report/authz/docx tests pass, including
      the 12 in `test_report_docx.py` and 11 in `test_scribble_report_authz.py`.

## Remaining

- [ ] Once merged, re-vendor into lotek (`scripts/stage-extension.sh`) so the vendored copy in
      `lotek`'s `extensions/scribble/` picks up the real fix instead of a local hand-patch.

## Notes / gotchas

- Mirrors `#6`'s exact lesson: the HTML route's guard is correct and centralized
  (`_authorize_engagement_view`), but a route that forgets to CALL it fails open, not closed — and
  `#6` already proved a stub/test host that omits `can_view_client` masks this (deny-only assertions
  pass against a route that's either fully gated or not gated at all). The new test here explicitly
  asserts an ALLOW case (200 for the client the actor DOES hold membership under), not just a DENY
  case, so it cannot pass against a route that is simply always-404.
