# feat/registrar-staged-ui

- **Status:** ready for review

## Purpose
Fixes ext#140. Registrar's confirm-tier workflow is "an agent stages; a human approves in the
dashboard" — but the dashboard had no way to do it (one route, three read-only tables). This adds the
human half: a Staged-actions queue with Approve/Reject, reachable and audited.

## Done
- `service.reject()` — decline a staged action without executing it; same interactive+write+operator
  gates as `approve`, minus the confirmer≠initiator rule (rejecting runs nothing). Audited (`rejected`).
- `blueprint.py` — dashboard passes the visible staged rows (with an `approvable` flag = initiator is
  not the current actor); two no-JS form-POST routes `/staged/<id>/approve` and `/staged/<id>/reject`
  that call the service, scope the row (`_load_visible_staged_or_404`), and redirect with a banner.
- `list.html` — Staged-actions section with per-row Approve/Reject forms (CSRF, confirm dialog); the
  initiator sees "you staged this" and no Approve button.
- Machine surface untouched — no approve/reject route on `api_pat.py` (INV-EXT-02 preserved).
- Tests (`tests/test_staged_ui.py`, 8) + REGISTRAR.md updated.

## Evals
- Dashboard lists a staged action with Approve/Reject; initiator sees no Approve.
- Approve executes + audits; Reject sets rejected + audits.
- Server-side refusals (route-level, not just UI): initiator, non-interactive, viewer.
- Tenancy: a staged row outside the actor's scope 404s on approve/reject.

## Notes/gotchas
- Approve/Reject are on the cookie-authed UI blueprint `bp`; a PAT is non-interactive so `service`
  refuses it even if it reached the route. No machine approve route added.
- Red-then-green transcripts captured for reject-status, tenancy-404, and initiator-hide.
