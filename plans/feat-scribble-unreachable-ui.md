# feat/scribble-unreachable-ui

- **Status:** ready for review

## Purpose
Fixes ext#141, ext#142, ext#143 — three scribble surfaces built on the machine API with no browser
reach. Same class as core #546/#547/#549: backend exists, no template/cookie-route calls it.

## Done
- **#143 batch move:** cookie `POST /scribble/api/engagements/<id>/findings/move` (sibling of the
  machine bulk-move) + board multi-select (checkboxes, select→bulk bar "Move selected to…"). Atomic:
  every id must belong to the engagement or nothing moves. Single drag untouched.
- **#141 attack-path linking:** cookie `POST .../attack-paths` (link) + form `POST .../attack-paths/
  <id>/unlink`. Scribble has no seam to vector, so board.js talks to vector's cookie API directly —
  lists the author's diagrams (tenancy-scoped by vector), fetches `export.html`, POSTs the snapshot.
  New board "Attack paths" section lists linked diagrams + unlink.
- **#142 vuln-map:** `/scribble/library/vuln-map` page — list / add / delete the source·title·dedupe →
  template mappings `promote_job` resolves through. No-JS form POSTs, write-gated, reachable from the
  library header.
- Tests: `scribble/tests/test_unreachable_ui.py` (13). Docs: SCRIBBLE.md.

## Evals
- #143: several findings move in one atomic call; a foreign id moves nothing; board renders checkboxes.
- #141: link creates a diagram; embed_html required; unlink removes it and 404s across engagements;
  board renders the section + picker.
- #142: page lists + add/delete persist; a template and ≥1 match key are required.

## Notes/gotchas
- board.js hardcodes vector's mount at `/vector` via `data-vector-base` (the conventional prefix;
  overridable on the section element).
- Red-then-green transcripts: batch atomicity, unlink tenancy, vuln-map match-key.
