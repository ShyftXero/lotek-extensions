# Plan: feat/scribble-machine-authoring

- **Branch:** `feat/scribble-machine-authoring` (off `origin/main`)
- **Companion:** `ShyftXero/lotek` branch `feat/pat-lifecycle-clients-idempotency` (core half: clients CRUD, host `idempotent` seam, visibility owner path)
- **Status:** 🟡 in progress

## Purpose
Give a write-scoped PAT the scribble/cream surface it lacks so an agent can author a full pentest
report headlessly: author custom findings (not just instantiate templates), create reusable vuln
templates, list/resume engagements, and render the deliverable over the API.

## Done
- [x] `scribble/prosemirror_sanitize.py` — allowlist sanitizer for PAT-supplied `content_json` (XSS boundary), **plus a recursion-depth cap** (an uncapped walker 500s on adversarially-nested JSON — INV-INPUT-02) + red-then-green tests incl. depth.
- [x] `scribble_add_finding` THIRD branch: author from `{title, severity, description, remediation, cvss_vector?, references?, target_*?}`; plain text via `doc_from_text` OR sanitized `content_json`.
- [x] `POST /machine/templates` (+ `content_json` declared on `CreateTemplateRequest` — the handler accepted it but the schema omitted it, so it was undiscoverable from the OpenAPI spec).
- [x] `GET /machine/engagements` (list) + `GET /machine/engagements/{id}`.
- [x] `GET /machine/engagements/{id}/report?format=html|docx` — read scope, `can_view_engagement`, audit read event. NO pdf.
- [x] scribble + cream creates wrapped in the host `idempotent` seam — including the `template_id` branch of `add_finding`, which returned in-session and so silently minted duplicates on a keyed retry (it has no natural dedup key, unlike the `lotek_finding_id` branch's `source_finding_id`).
- [x] **`source_finding_id` / `asset_id`: `Integer` → `SoftHostId`** — they hold CORE ids, which are UUIDs on v2. See the CLAUDE.md section this branch added.
- [x] Tests green: sanitizer 16, cream idempotency 6; full mounted lifecycle passes in lotek.

## Remaining
- [ ] Mounted-host coverage for the audit rows + scribble idempotency (the stub host injects neither seam, so those only prove out mounted in lotek).
- [ ] PR → squash-merge → release tag → lotek re-pins to that tag.

## Notes / gotchas
- scribble/cream ship as PINNED GIT DEPS into lotek (`ShyftXero/lotek-extensions` tag). This branch is
  consumed only after: merge here → **tag** → lotek bumps its pin to that tag. The lotek gate must run
  against the TAG, not a local path-override, or it proves unreleased code.
- `can_view_engagement` = `can_view_client(engagement.client_id)` — engagement visibility is CLIENT
  visibility. The core `owner_id` fix (companion branch) is what unblocks create/author/render here;
  nothing membership-side is needed in scribble.
- Report render is **synchronous** (no job queue): the report endpoint is a single sync GET, not
  POST-render-then-GET. HTML + docx only — server has no PDF renderer.
- `content_json` from a PAT is untrusted → MUST pass the sanitizer before persist (add_finding AND
  templates). Template `content_json` was previously admin-only; the PAT path is a new trust boundary.
- Keep cream issue/void human-only; do not add them. Idempotency wraps are the only cream change.
