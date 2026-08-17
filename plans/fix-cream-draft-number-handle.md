# Plan: fix/cream-draft-number-handle

- **Branch:** `fix/cream-draft-number-handle`  (worktree: `.claude/worktrees/ux-cream-number`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Close ext#46 (client-reported, reproduced from a real PAT-driven deliverable session): in cream's
document list a **draft**'s `Number` cell renders as a bare `—`. The link is present and navigable
(`templates/cream/list.html:28` wraps it in `url_for('cream.view_document', doc_id=d.id)`) — the defect
is what the cell *says*: `cream/blueprint.py:122` builds `"number": d.number or "—"`, so for an unissued
document the anchor's entire text is a one-character em-dash. No identity, and a near-invisible click
target.

Fix: show a stable, tail-truncated UUIDv7 handle — `draft …b839c91e20` — matching the convention
lotek#336 specifies for a core-owned UUID reference widget, and keep the whole cell clickable.

## Done

- [ ] filled in as work lands

## Remaining

- [ ] filled in as work lands

## Notes / gotchas

- **Tail, never prefix.** UUIDv7's leading 48 bits are a millisecond timestamp; lotek#336 measured five
  consecutively created ids sharing their first **23** characters (1 distinct first-8 of 5, 5 distinct
  last-8 of 5). A "short id" taken from the front does not discriminate, and it fails *silently*.

## Red-then-green

(recorded below as each guard is added)
