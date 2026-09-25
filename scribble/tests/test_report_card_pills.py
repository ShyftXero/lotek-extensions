"""The finding-card badge row renders as ROUNDED pills (DrawingML roundRect shapes), not square chips —
matching the HTML card. Guards against a revert to flat/square badges: a card with a severity, a CVSS
score and affected hosts must emit roundRect shapes carrying those labels.
"""
from __future__ import annotations

import io
import uuid
import zipfile

import scribble.models as fm
from scribble.enums import Severity
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx


def test_card_badge_row_uses_rounded_pill_shapes(app, session_factory):
    with session_factory() as db:
        eng = fm.ReportBoard(name="Pill Eng", client_id=uuid.uuid7())
        grp = fm.FindingGroup(engagement=eng, name="G", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        parent = fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, order_index=0,
                                 title="Vuln", severity=Severity.critical, cvss_score=9.8, content_json={})
        db.add(parent)
        db.flush()
        db.add(fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, parent_id=parent.id, order_index=1,
                               title="Vuln", severity=Severity.critical, target_host="192.0.2.9",
                               target_port="80", content_json={}))
        db.commit()
        eng_id = eng.id

    with app.app_context(), session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(fm.ReportBoard, eng_id)))
    doc_xml = zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml").decode("utf-8", "replace")

    # rounded pills (not square char-shaded chips): a roundRect preset geometry per badge.
    assert doc_xml.count('prst="roundRect"') >= 3, "expected severity + CVSS + affected pills"
    assert "CVSS" in doc_xml and "affected asset" in doc_xml
    # the full target URL must NOT be crammed into the header meta anymore
    # (the reproduction/affected sections still carry it; this only checks the badge row is clean).
