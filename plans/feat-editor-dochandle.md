# Plan: feat/editor-dochandle

- **Branch:** `feat/editor-dochandle`  (worktree: `.claude/worktrees/pr-323`, off `main`)
- **PR:** #323 — https://github.com/ShyftXero/lotek-extensions/pull/323
- **Status:** 🟢 ready to merge

## Purpose
Phase 1c of ext#321 (unified autosave seam). Teach the shared kit reporting-editor to address an opaque
`{kind,id,block}` **doc handle**: with one, autosave + load target the host `/_kit/autosave` seam (landed
in phase 1a, PR #322); without one it keeps the EXACT legacy `apiBase/findings` route and raw-doc body, so
the live finding editor is unchanged. This is the client half that lets a NEW surface mount the house editor
with zero new backend — the server half already exists and is tested.

## Done
- [x] `contentTarget(state)` — the single predicate that computes the save/load target: seam when a
      `docHandle` is present, legacy per-finding route when only `findingId` is, `null` otherwise
      (a form-submit adopter). `scheduleSave`/`saveNow`/`fetchLatest`/`pendingKey` all route through it.
- [x] `docHandle` resolved from `opts.docHandle` or `data-doc-kind/-doc-id/-doc-block`; `findingId` still
      drives presence + inline-image uploads either way.
- [x] Legacy branch is byte-identical: same URL, same raw-doc body, same `pendingKey`
      (`findingId:block`) — the live finding editor is NOT flipped onto the seam.
- [x] Browser-gated routing test `test_dochandle_routes_autosave_to_the_seam_else_legacy` pins all three
      cases (seam URL+body+load, legacy URL+body, none→null).
- [x] Tests green: `kit/tests/test_reporting_editor_dom.py` 40 passed (incl. new routing test);
      `kit/tests/test_autosave_seam.py` 8 passed (server contract unchanged). ruff + pyrefly clean.

## Remaining
- [ ] Nothing for THIS branch. The first real consumer (phase 1b findings migration, phase 2 library, or
      phase 5 engagement prose) is tracked in ext#321 — see decision below.

## Decision: land the seam half standalone (path a), not block on a consumer
The author's original DRAFT note said this should land with its first real consumer so the seam path runs
end-to-end + is re-pinned into core. On review that bar belongs to the first-consumer PR, not this one:

- **Additive + back-compat + reversible.** The new seam branch is dead code until a surface sets a
  `docHandle`; nothing does yet, so there is zero production path change for the seam. The ONLY live change
  is routing the existing finding editor through `contentTarget`, whose legacy branch is pinned
  byte-identical by the routing test and whose rendering is covered by the full DOM suite.
- **Consistent with phase 1a.** The server half (PR #322) landed standalone with no consumer; this is the
  symmetric client half.
- **The client/server contract is cross-pinned.** The routing test fixes the exact client body/URL
  (`{kind,id,block,doc}` → `POST /_kit/autosave`; `?kind&id&block` → `GET`); `test_autosave_seam.py` fixes
  the exact server-accepted shape. The two together verify the contract without a heavyweight
  browser+Flask e2e.
- **Building a consumer here would leapfrog a deferred decision.** phase 1b in ext#321 is explicitly open
  ("Decide: who mounts the seam — core host vs an extension — + the host actor resolver"). A real consumer
  needs that mount/actor decision; coupling it to this 73-line seam PR would smuggle a larger architectural
  call into it. The end-to-end exercise + re-pin into core happens there, on purpose.

## Notes / gotchas
- Pre-existing reds in `kit/tests/test_port_parity.py` (2) are from `lotek_kit.attackpath` drifting from
  `vector/vector/schema.py` — unrelated to this branch, flagged in ext#321.
- Precedence when both are present: `docHandle` wins for save/load; `findingId` still drives presence.
  No current page sets both.
