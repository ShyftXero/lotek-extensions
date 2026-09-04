"""#627 — machine-readable CSV export of an engagement's findings.

One row per report-included finding (per-host children flattened to their own rows, marked by
``parent_id``), serialized from the SAME ``ReportContext`` the deliverable renders — no parallel data
path — so a spreadsheet / SIEM import matches the HTML/docx report. Columns are the current
``FindingCtx`` scalar DTO fields plus the group it belongs to and its #626 evidence SHA-256s.
"""

from __future__ import annotations

import csv
import io

from scribble.reporting.context import FindingCtx, ReportContext

# Column order IS the published contract — append, never reorder. TODO(#624/#625): reference/CWE columns
# land with those tickets; the DTO does not carry them in this repo yet. Do NOT fork the column set here.
_COLUMNS = (
    "finding_id",
    "parent_id",
    "group",
    "title",
    "severity",
    "cvss_score",
    "cvss_vector",
    "target_host",
    "target_port",
    "target_url",
    "evidence_count",
    "evidence_sha256",
)


def _row(f: FindingCtx, group_name: str, parent_id) -> dict:
    return {
        "finding_id": f.id,
        "parent_id": parent_id if parent_id is not None else "",
        "group": group_name,
        "title": f.title,
        "severity": f.severity,
        "cvss_score": "" if f.cvss_score is None else f.cvss_score,
        "cvss_vector": f.cvss_vector or "",
        "target_host": f.target_host or "",
        "target_port": f.target_port or "",
        "target_url": f.target_url or "",
        "evidence_count": len(f.artifacts),
        # #626 hashes, space-joined; empty for pre-hash rows. One cell keeps every row flat + rectangular.
        "evidence_sha256": " ".join(a.sha256 for a in f.artifacts if a.sha256),
    }


def _rows(ctx: ReportContext):
    for g in ctx.groups:
        for f in g.findings:
            yield _row(f, g.name, None)
            for c in f.children:  # a nested per-host child becomes its own row, tagged with parent_id
                yield _row(c, g.name, f.id)


def render_report_csv(ctx: ReportContext) -> str:
    """Serialize ``ctx``'s findings to a CSV string with a header row (see module docstring)."""
    # ponytail: no spreadsheet formula-injection escaping (a cell like `=cmd()` from a scan-derived
    # host/title stays verbatim). This export targets SIEM/programmatic ingestion, which does not
    # evaluate formulas; prefixing `'` would corrupt the value for that primary consumer. Add
    # OWASP-style escaping here IF a spreadsheet becomes the primary consumer.
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS)
    writer.writeheader()
    for row in _rows(ctx):
        writer.writerow(row)
    return buf.getvalue()
