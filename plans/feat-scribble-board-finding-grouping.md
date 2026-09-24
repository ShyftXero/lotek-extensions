# Plan: feat/scribble-board-finding-grouping

- **Branch:** `feat/scribble-board-finding-grouping` (off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Phase 1b of finding-grouping (lotek core Phase 1 = PR #829). A report board can hold hundreds of
findings — an adopted job with fleet-wide vulns (EternalBlue on every host, self-signed certs, LLMNR)
floods the board as one flat row per instance (measured: 430 rows, 21k px tall, no pagination). Give the
board a **By vulnerability / By host** toggle that collapses same-kind findings into one bucket with a
host count — mirroring the core per-job report's toggle, and consuming the **same** core bucketer
(`app.finding_grouping`) through the host seam so the two surfaces can never drift.

CVE is an attribute of a kind, not its own axis (user steer; a By-CVE view was built then dropped in
#829). The grouping key is the vuln KIND = `dedupe_key` else `source:title`.

## Done
- [x] Bulk-bar unstyled-control CSS fix (rider): `.scribble-bulk-bar select/input` now inherit the field
      base rule — the last straggler the `ux-eval/walk.py` unstyled-control detector flagged (0 left).

## Remaining
- [ ] `scribble/finding_grouping_adapter.py` — pure `board_finding_to_group_row(f) -> dict` (id, host,
      cves, severity, kind_key=`source_facts["dedupe_key"] or source:title`, label, payload).
- [ ] `scribble/host.py` — `group_findings(rows, by)` wrapper over `host_hook("group_findings")`;
      returns `[]` when unmounted (board tabs hide).
- [ ] `engagement_ui.py` board route — build rows from the FLATTENED finding set (parents' per-host
      children incl., via findings_service) → `host.group_findings(rows, by=...)`; pass
      `findings_by_kind` / `findings_by_host` to the template.
- [ ] `templates/scribble/engagement.html` — three client-side tabs (Board groups / By vulnerability /
      By host) mirroring core `report.html`; bucket rollup tables link to `#finding-<id>`.
- [ ] Cross-surface behavioural drift test — one input set through the core adapter (reporting.py) AND
      the scribble adapter → identical buckets. Lives in **core** tests (`test_scribble_ui_mounted.py`),
      the one place both adapters + the bucketer are importable. Ships with the core re-pin.

## Notes / gotchas
- **Seam, not lotek-kit.** `finding_grouping` is value-typed to cross the core↔scribble seam. Moving it
  to kit would disturb shipped #829 + force a kit re-release across every extension. The core seam
  (`group_findings` in `extensions.py`) is the house style; ext must not `import app.*`.
- **Standalone degrades:** `host_hook` is None off-mount → wrapper returns `[]` → the By-vuln/By-host
  tabs hide. The board's default assessment-type group view is unchanged and always present.
- **`cve_ids` is already normalized** (metadata.normalize_cve_ids) — pass straight into the row, do NOT
  double-normalize (different cap semantics than core's normalize_cves).
- **`dedupe_key` is not a column** — it lives in `source_facts` JSON (dispositions.py).
- Core companion branch: `feat/scribble-board-finding-grouping` (seam + walk.py detector + re-pin +
  mounted eval).
