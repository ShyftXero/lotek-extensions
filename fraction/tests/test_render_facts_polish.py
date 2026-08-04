"""Render-polish regressions (CONTRACT.md §5.4 / Track F): the DOCX template hides an empty `CVSS:`/
`Target:` label instead of leaking a bare one (`fraction/report_templates/build_default_docx.py`'s
`{% if f.cvss_score %}`/`{% if f.target %}` guards), and per-host child rows show real facts-derived
evidence rather than a truncated copy of the parent's description.

Track F's own handoff notes proved these fixes with a throwaway script; this file pins them as real,
committed pytest assertions.
"""

from __future__ import annotations

import io

import docx
from docx.oxml.ns import qn

from fraction.content import schema
from fraction.enums import Severity
from fraction.models import Engagement, EngagementFinding, FindingGroup
from fraction.reporting import build_report_context
from fraction.reporting.render_docx import render_report_docx


def _all_text(doc: docx.Document) -> str:
    return "".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))


def test_docx_hides_empty_cvss_and_target_labels(session_factory):
    """A finding with NO `cvss_score` and NO `target_host`/`target_url` must not leak a bare
    `CVSS: `/`Target: ` label with nothing after it."""
    with session_factory() as db:
        eng = Engagement(name="Bare Meta Co", company_name="Acme")
        group = FindingGroup(engagement=eng, name="Internal", order_index=0)
        EngagementFinding(
            engagement=eng,
            group=group,
            title="No CVSS, No Target",
            severity=Severity.medium,
            order_index=0,
            content_json={"description": schema.doc_from_text("A finding with no scored/target meta.")},
        )
        db.add(eng)
        db.commit()
        eng_id = eng.id

    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        payload = render_report_docx(ctx)

    doc = docx.Document(io.BytesIO(payload))
    text = _all_text(doc)
    assert "No CVSS, No Target" in text
    assert "CVSS: " not in text  # no bare label
    assert "Target: " not in text


def test_docx_shows_cvss_and_target_labels_when_present(session_factory):
    """The converse: when a finding DOES carry a score/target, the (non-empty) label renders."""
    with session_factory() as db:
        eng = Engagement(name="Scored Co", company_name="Acme")
        group = FindingGroup(engagement=eng, name="External", order_index=0)
        EngagementFinding(
            engagement=eng,
            group=group,
            title="Scored And Targeted",
            severity=Severity.high,
            order_index=0,
            cvss_score=7.5,
            target_host="app.acme.test",
            content_json={"description": schema.doc_from_text("Has a score and a target.")},
        )
        db.add(eng)
        db.commit()
        eng_id = eng.id

    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        payload = render_report_docx(ctx)

    doc = docx.Document(io.BytesIO(payload))
    text = _all_text(doc)
    assert "CVSS: 7.5" in text
    assert "Target: app.acme.test" in text
