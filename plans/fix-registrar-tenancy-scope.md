# Plan: fix/registrar-tenancy-scope

- **Branch:** `fix/registrar-tenancy-scope`  (worktree: `.claude/worktrees/a4-registrar-tenancy`, off `main`)
- **PR:** not opened yet (handoff to PR-gate reviewer per agent-ipc)
- **Status:** 🟡 in progress

## Purpose
Close INV-TENANCY-06 cross-tenant read leak (issue #73). The registrar list endpoints
`/domains`, `/staged`, `/audit` returned ALL rows with no engagement filtering — a caller in
engagement A could read engagement B's domains, staged actions, and audit. Present on all three
surfaces: human JSON API (`registrar/api.py`), PAT machine mirror (`registrar/api_pat.py`), and the
dashboard (`registrar/blueprint.py`).

## Done
- [ ] `service.visible_domains` / `visible_staged` helpers, mirroring the existing `visible_servers`
      scoping seam exactly (same signature; unowned/unbound rows are org-level → admin-only).
- [ ] `/domains` + `/staged` scoped by `Domain.checked_out_to` / `StagedAction.engagement_id` on all
      three surfaces via `host_visible_engagement_ids()` + admin signal.
- [ ] `/audit` made admin-only (AuditRecord has NO engagement_id column, so it cannot be
      engagement-scoped by row — see written reason in code) on all three surfaces.
- [ ] Tests: no-membership caller sees none of another engagement's domains/staged/audit; a member
      sees only theirs; admin sees org inventory. Red vs old (unscoped), green vs fix.

## Remaining
- [ ] Local ruff + pyrefly + pytest green for registrar subproject.
- [ ] Handoff on #73 (do NOT open PR / self-merge).

## Notes / gotchas
- `AuditRecord` has no engagement column → engagement-scoping impossible → admin-only is the honest
  fix (task allowed either). Non-admin gets an empty `audit=[]` (200), consistent with the
  filter-don't-reject pattern the sibling list endpoints use, not a 403.
- Unowned domain (`checked_out_to is None`) / unbound staged (`engagement_id is None`) = org-level
  inventory → treated like `visible_servers`' `static` kind: admin-only (standalone sees all).
- Consumer note for the lotek re-pin: `lotek/tests/test_registrar_extension.py:191` reads
  `/registrar/api/audit` as an OPERATOR (op2); after this change that returns `[]`, so its
  secret-free assertion passes VACUOUSLY. Flag for the re-pin PR to switch that read to an admin.
- Existing machine tests that read `/audit` as the default operator PAT actor were updated to mint
  an admin actor (audit is now admin-only).
