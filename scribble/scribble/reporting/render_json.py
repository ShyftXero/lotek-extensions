"""#627 — machine-readable JSON export of an engagement report.

A structured serialization of the SAME ``ReportContext`` the HTML/docx renderers consume — no parallel
data path — so a JSON export can never disagree with the deliverable it mirrors. Emits engagement
metadata, the severity rollup, and every report-included finding (per-host children nested under their
parent, exactly as the report renders them), each with its evidence's #626 SHA-256 so a consumer can
verify the referenced files.

Scalar DTO fields only: the authored prose (``FindingCtx.blocks_html``) is *rendered HTML*, not
structured data, so it is deliberately not dumped here — a consumer wanting the narrative reads the
HTML/docx deliverable. Columns track the current ``FindingCtx`` DTO; the #624/#625 reference/CWE fields
are not in this repo's DTO yet (see the TODO slot in :func:`_finding`).
"""

from __future__ import annotations

import json

from scribble.reporting.context import ArtifactCtx, FindingCtx, ReportContext


def _evidence(artifacts: list[ArtifactCtx]) -> list[dict]:
    return [
        {
            "filename": a.filename,
            "caption": a.caption,
            "content_type": a.content_type,
            # #626 evidence-integrity hash, carried verbatim (None for a row persisted before hashing).
            "sha256": a.sha256,
        }
        for a in artifacts
    ]


def _finding(f: FindingCtx, group_name: str) -> dict:
    return {
        "id": f.id,
        "group": group_name,
        "title": f.title,
        "severity": f.severity,
        "cvss_score": f.cvss_score,
        "cvss_vector": f.cvss_vector,
        "target_host": f.target_host,
        "target_port": f.target_port,
        "target_url": f.target_url,
        "facts_line": f.facts_line,
        # TODO(#624/#625): reference[]/CWE columns land with those tickets; the DTO does not carry them
        # in this repo yet. Do NOT fork the column set here — extend it when the DTO grows the fields.
        "evidence": _evidence(f.artifacts),
        "children": [_finding(c, group_name) for c in f.children],
    }


def render_report_json(ctx: ReportContext) -> str:
    """Serialize ``ctx`` to a pretty-printed JSON string (see module docstring).

    ``default=str`` renders the UUIDv7 finding/engagement ids (and any other non-JSON scalar) as their
    string form, which is how every other scribble machine surface already emits an id.
    """
    doc = {
        "engagement": {
            "id": ctx.engagement_id,
            "name": ctx.engagement_name,
            "company_name": ctx.company_name,
            "client_name": ctx.client_name,
            "scope_type": ctx.scope_type,
            "start_date": ctx.start_date,
            "end_date": ctx.end_date,
        },
        "rollup": {
            "counts": ctx.rollup.counts if ctx.rollup else {},
            "total": ctx.rollup.total if ctx.rollup else 0,
            "overall": ctx.rollup.overall if ctx.rollup else "info",
            "risk_override": ctx.risk_override,
            "risk_override_rationale": ctx.risk_override_rationale,
        },
        "findings": [_finding(f, g.name) for g in ctx.groups for f in g.findings],
        # Engagement-level evidence appendix (artifacts with no finding_id) — the same list the report's
        # Evidence appendix publishes, carried so its #626 hashes are exportable too.
        "evidence": _evidence(ctx.artifacts),
    }
    return json.dumps(doc, indent=2, default=str)
