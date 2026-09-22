# feat/bugreport-adopt-kit-editor

- **Status:** in progress

## Purpose

Bugreport's report body is a plain `<textarea>` with images only via a separate attachment upload.
Adopt the shared `lotek-kit` reporting editor (ProseMirror-JSON, inline image paste) exactly the way
scribble already does — the kit docstring names bugreport as a "later" adopter. One canonical editor,
image paste into the body, back-compatible with existing plain-text bodies.

## Done

- **`render_html.py`** — ProseMirror-JSON → sanitized HTML by construction (escape every value, tag
  allowlist, artifact-only images incl. protocol-relative rejection, scheme-checked links, depth
  bound). Back-compat: legacy plain-text bodies render escaped; `body_to_doc` opens them in the editor.
- **Artifact endpoints** — `POST /<report_id>/api/artifacts` (editor paste sink → `service.attach`,
  returns `{id, url}`) + `GET …/artifacts/<id>/raw` (`send_attachment`, report-ownership 404). Same
  25 MiB cap / magic-byte sniff / reporter+admin visibility as a manual attachment.
- **Kit** — gated the editor's per-block autosave/fetch/presence loop on a finding-id, so a form-submit
  host (bugreport) mounts without one and makes no background requests (scribble unchanged).
- **Frontend** — create + edit forms mount the editor (no finding-id); a real `<textarea name="body">`
  is the field JS hides + serializes `getDoc()` into (no-JS still works, edit round-trips); read view
  renders `bugreport_render_body`. Inline paste works on edit (report exists); create is rich-text only
  (attachments need a saved report — bugreport's own model). `/_kit` from the host; tests use a shim.
- Tests at every layer green (render, endpoints, kit gate, UI render, no-JS); red→green transcript
  captured. Two issues found + fixed in review: protocol-relative image src, no-JS body wipe.

## Remaining

- Reviews + `--ack-*`; ext PR; merge; auto-tag; re-pin **kit + bugreport** into lotek core ("fix the
  pin"); mounted tests + core PR.

## Notes / gotchas

- Core serves the kit editor at `/_kit` (lotek#750). Scribble's pin in core is currently
  `v2026.9.10.234653+gca07598` (pre-adoption) — the adoption + re-pin happen together here for
  bugreport.
