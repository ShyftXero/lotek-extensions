# Plan: feat-pastel

- **Branch:** `feat/pastel`  (worktree: `.claude/worktrees/feat-pastel`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Land the PASTEL paste extension (lotek-extensions#369). Builds the full extension: unguessable-slug paste
store (markdown/text, `/raw` view, runner→pastel machine API) plus the access-control layer from #369
(expiry, read-count limit, HTTP Basic password auth).

## Done
- [x] Worktree + branch created off `main`
- [x] Plan file committed first

## Remaining
- [ ] Write all extension files (models, service, blueprint, machine API, templates, tests)
- [ ] `ruff` + `pytest` green
- [ ] Security + adversarial review acks
- [ ] Open PR → `main`

## Notes / gotchas
- No base PASTEL code existed anywhere; building from scratch following bugreport pattern.
- issue #369 describes access controls as a follow-up to a base build "shipping separately" — since
  no base code exists, both base and access controls are landed in this single branch.
- Slug is `secrets.token_urlsafe(32)` (256-bit CSPRNG), NOT UUIDv7 — avoids time-ordered guessing.
- Read-count increment is atomic (single UPDATE … RETURNING) so concurrent requests cannot
  double-serve past the limit.
- Password uses `werkzeug.security.generate_password_hash` / `check_password_hash` (bundled with Flask,
  no new dep). HTTP Basic auth challenge: 401 + `WWW-Authenticate: Basic realm="Paste"`.
- Markdown rendering is NOT included (no stdlib parser, don't want new dep for a sprint). Body displayed
  in `<pre>`. Follow-up to add mistune/markdown-it.
