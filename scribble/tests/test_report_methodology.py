"""The docx renders the standing Methodology section + severity-rating definitions (HTML parity). The
.docx was missing both; this pins that they appear, reusing render_html's shared standing constants."""
from __future__ import annotations

import io
import uuid
import zipfile

import scribble.models as fm
from scribble.enums import Severity
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx


def test_docx_appends_methodology_and_severity_ratings(app, session_factory):
    with session_factory() as db:
        eng = fm.ReportBoard(name="Method Eng", client_id=uuid.uuid7(), scope_type="external")
        grp = fm.FindingGroup(engagement=eng, name="Perimeter", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        db.add(fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, order_index=0, title="V",
                               severity=Severity.critical, content_json={}))
        db.commit()
        eng_id = eng.id

    with app.app_context(), session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(fm.ReportBoard, eng_id)))
    text = zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml").decode("utf-8", "replace")

    assert "Methodology" in text
    assert "Scoping and rules of engagement" in text     # a standing phase
    # Severity ratings are their OWN movable block now (#Q1), not a sub-heading of Methodology — the
    # heading is title-case "Severity Ratings". It still renders in the default order, so it's in the doc.
    assert "Severity Ratings" in text
    assert "Remediate immediately" in text               # the critical rating definition
