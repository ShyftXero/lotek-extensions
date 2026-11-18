# Plan: feat/pastel

- **Branch:** `feat/pastel`  (worktree: `.claude/worktrees/pastel`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Land the **PASTEL** paste extension: markdown pastes behind unguessable GUID URLs, a `/raw`
view, autosave in the editor, a runner→pastel machine API, PLUS the three access controls from
ShyftXero/lotek-extensions#369 — per-paste **expiry**, **read-count limit**, and **password**
(HTTP Basic auth). The three compose and every gate fails closed.

## Done
- [ ] nothing yet

## Remaining
- [x] package skeleton (pyproject, manifest, __init__/register, db, config, deps, host, _version)
- [x] models: `pastel_pastes` (guid, body, author, access-control columns)
- [x] render_html: markdown -> nh3-sanitized HTML
- [x] service: create / load / retire + atomic read-count gate + expiry + password verify
- [x] blueprint: editor + owner list + authed preview (no count) + public GUID view/raw
- [x] api_pat + api_schemas: runner machine API
- [x] templates: base, list(editor), view
- [x] tests: access controls (the #369 graders), manifest
- [ ] ruff + pyrefly + pytest green
- [ ] PR into main

## Notes / gotchas
- **"Read" counts only on the PUBLIC GUID fetch** (`/p/<guid>` + `/p/<guid>/raw`), never the authed
  owner preview — keyed on the unguessable GUID, per #369.
- **Atomic read-count** via a conditional `UPDATE ... WHERE read_count < max_reads` + `rowcount`;
  no TOCTOU double-serve past the cap under concurrent fetches.
- **Expired == unknown == over-limit == 404** (no existence oracle). Expiry is lazy-check-on-read
  (primary) plus a `sweep_expired()` helper; no background job.
- **Password** stored as a werkzeug hash, constant-time compare, challenged with `WWW-Authenticate:
  Basic`; applies to rendered view AND `/raw`.
- Editor is a markdown `<textarea>` + localStorage autosave + live preview — NOT the heavy kit
  ProseMirror editor. `ponytail:` deliberate; upgrade to the kit editor if rich-text is needed.
- Mounted test in lotek (`tests/test_pastel_extension.py`) is a follow-up; the atomic-count
  correctness is proven in this extension's own SQLite suite.
