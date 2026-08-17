# Plan: fix/scribble-report-render-sweep

- **Branch:** `fix/scribble-report-render-sweep`  (worktree: `.claude/worktrees/ux-report-sweep`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Four **client-reported, reproduced** defects in scribble's HTML report renderer, surfaced by an agent
driving the lotek PROD instance over a PAT to author a real TeamsPlus deliverable. Triage notes +
repro scripts + PDF/PNG evidence: `~/tmp/teamsplus_lotek_notes/lotek_triage/`.

| issue | defect |
|---|---|
| ext#39 | the severity block loses all colour in print/PDF (`@media print` never sets `print-color-adjust`; `--sev-*` not pinned for paper) |
| ext#42 | Methodology vanishes with no checklist, leaving a live nav link to an empty anchor |
| ext#45 | back-nav (`← Dashboard` / `← Back to engagement`) sits inside the document masthead, not the toolbar |
| ext#40 | image evidence never renders for a nested-CHILD finding, nor for an ENGAGEMENT-level artifact (`finding_id` null) — both silent, both 201 |

## Done
- [ ] (filled in as each lands)

## Remaining
- [ ] #39 print colour
- [ ] #42 methodology default + conditional nav link
- [ ] #45 nav into the toolbar
- [ ] #40 child gallery + engagement-level evidence + effective `finding_id` echo

## Notes / gotchas
- (to be filled)

## Red-then-green
- (to be filled)

<!-- Lifecycle: create + commit this FIRST thing when cutting the branch; keep it current; KEEP it —
     it merges to main with the branch as a durable record (since 2026-07-28; see CLAUDE.md
     "Per-branch plan"). -->
