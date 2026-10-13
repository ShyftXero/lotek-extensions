# feat/board-collapse-children

- **Status:** built, pending release decision (batch with auto-section?)

## Purpose
The engagement board rendered every per-host child as its own card (198 cards for 9 vulns = the "not
grouping" prod complaint). Collapse per-host children under their parent: hide children from the board's
top-level lists and show a "N hosts" count pill on the parent card. Same nesting the report already uses
(findings_service.nested_child_ids) — ONE rule.

## Done
- engagement_ui.py engagement_board: compute nested_ids + child_counts; board_groups/ungrouped exclude
  nested children; pass nested_ids/child_counts to the template.
- engagement.html: both finding loops skip children (filtered in context) + show "{N} hosts" pill on parents.
- test_board.py::test_board_collapses_children_under_their_parent. 75 related tests green, ruff clean.

## Notes / ceiling
- Children are HIDDEN (count only), not expandable-in-place. Detail stays on the parent's finding page +
  the "Grouped by vulnerability" tab. Add a disclosure/expand later if wanted.
