# feat/scribble-report-collapse-cards — collapse the report's detailed section by vulnerability

- **Status:** in review — Stage 1 (card + TOC collapse, print pagination) code-complete, report tests green.

## Purpose

A real external engagement (scrubbed for dev as "Northwind") had **134 per-host findings that are only
17 distinct vulnerabilities** — one printer-unauth on 50 hosts, one Toshiba default-login on 15, etc.
Rendered one card per finding, that was a **73-page** deliverable of repeated write-ups and a **134-row
table of contents** (Eli: "too long with repeat blocks", "the print render is over-tall … particularly
the table of contents"). The rollup and the "at a glance" index already collapse by vulnerability; the
DETAILED section and the TOC did not.

## Done (Stage 1)

- `reporting/context.py`: `_collapse_by_kind` folds same-vulnerability top-level findings into ONE
  representative card (worst-severity first) with the rest as `children` (host instances). Keyed on
  `_kind_key` = CVE-set else normalized title — the SAME host-independent identity the board/rollup use
  (core `finding_grouping.kind_group_key`), never `target_host`/dedupe_key. Applied to both grouped and
  ungrouped sections. A report of all-distinct vulns is unchanged (nothing folds).
- Consequences, for free: the TOC (`_toc_entries` iterates `group.findings`) and the at-a-glance index
  both collapse to one entry per vulnerability; `_affected_assets` already aggregates hosts from
  `children`, and `_render_children` prints the "Affected hosts (N)" table inside the one card.
- `render_html.py` `_findings_index`: count hosts/CVEs/KEV across `[f, *f.children]` so the at-a-glance
  host count is the true fleet size after folding (was counting only the representative).
- `render_html.py` print CSS: a collapsed card can be taller than a page, so `.finding` /
  `.children-table` now break across pages (rows kept atomic, header repeats); and `details.children` is
  revealed in print (the hosts are the deliverable's content — a client PDF must show them).
- `tests/test_report_card_collapse.py`: seeds the scrubbed real distribution (17 kinds / 134 instances,
  synthetic — no client data) and asserts 17 cards, 17 TOC entries, and the 50-host card lists its hosts.
  Transcript: the test asserted 17 cards / 17 TOC entries and went RED at 134 against the pre-collapse
  renderer, GREEN after `_collapse_by_kind` landed.

## Remaining

- **Stage 2 (separate, needs a core host-seam method):** optional job-runs / scan-pipeline diagram in the
  Methodology section — exec-facing, default OFF, per-report toggle for color / status / time. The
  pipeline data (module runs, order/parallelism) lives in core; scribble reaches it via the host seam.
- Replay: offline HTML render of the scrubbed engagement + local-stack print-preview (both, per Eli).
- Then core re-pin, and release on Eli's call.
- Follow-up: `findings_service.rendered_top_level_count` (board/machine-API stat) still counts pre-collapse
  top-level findings; align it with the rendered card count if the board should report 17, not 134.
