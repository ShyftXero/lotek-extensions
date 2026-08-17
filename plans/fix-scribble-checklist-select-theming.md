# Plan: fix/scribble-checklist-select-theming

- **Branch:** `fix/scribble-checklist-select-theming`  (worktree: `.claude/worktrees/ux-checklist-theming`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
Close ShyftXero/lotek-extensions#44 — the engagement coverage-checklist panel's per-item status
controls render as **unstyled native `<select>` boxes** (white, OS chrome) against the dark themed
panel, one per checklist item. Client-reported from a real deliverable ("coverage checklist looks
broken while editing"). Two smaller defects in the same panel are in scope: the `.ckp-tray` dashed
rectangle that shows while the tray is `hidden`, and the undefined `ckp-tmpl` class on the template
buttons.

## Done
- [ ] plan committed

## Remaining
- [ ] `.ckp-status` themed
- [ ] tray `[hidden]` honoured
- [ ] `ckp-tmpl` resolved
- [ ] tests + red-then-green transcripts
- [ ] after-screenshot from the repro script

## Notes / gotchas
- (filled in as the work lands)
