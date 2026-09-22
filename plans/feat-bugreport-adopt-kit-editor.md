# feat/bugreport-adopt-kit-editor

- **Status:** in progress

## Purpose

Bugreport's report body is a plain `<textarea>` with images only via a separate attachment upload.
Adopt the shared `lotek-kit` reporting editor (ProseMirror-JSON, inline image paste) exactly the way
scribble already does — the kit docstring names bugreport as a "later" adopter. One canonical editor,
image paste into the body, back-compatible with existing plain-text bodies.

## Done

- (pending blueprint from the scribble reference)

## Remaining

- Mount `LotekReportingEditor` on the bugreport body field (create + edit), pointed at bugreport's
  artifacts endpoint for paste; serialize on save; render stored content back to a reader.
- Artifacts endpoint decision: reuse `POST /<id>/attachments` vs add an `/artifacts` route returning
  the shape the editor expects.
- Back-compat: existing plain-text bodies must still display.
- Tests (mounted + hermetic) + red→green transcripts; reviews + `--ack-*`; PR; merge; re-pin bugreport
  into lotek core ("fix the pin").

## Notes / gotchas

- Core serves the kit editor at `/_kit` (lotek#750). Scribble's pin in core is currently
  `v2026.9.10.234653+gca07598` (pre-adoption) — the adoption + re-pin happen together here for
  bugreport.
