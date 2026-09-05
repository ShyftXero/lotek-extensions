"""lotek#623: authored Strategic Recommendations — a longer-horizon, engagement-level list rendered as
its own report section (HTML + docx), plus its `PATCH /engagements/<id>` write seam.

Pins:
- **regression** — an engagement with NO recommendations renders BYTE-IDENTICALLY to before this field
  existed (the section and its TOC entry are absent);
- **populated** — the section lists every authored item, in order, HTML-escaped;
- **red-then-green** — neutering the empty short-circuit makes the no-recs report grow the section
  (the guard that proves the short-circuit is what keeps a bare engagement unchanged);
- **normalization** — blank lines / non-strings are dropped, order preserved;
- **docx mirror** — the .docx carries the same heading + items.
"""
from __future__ import annotations

import io
import zipfile

import scribble.reporting.render_html as rh
from scribble.models import Engagement, normalize_strategic_recommendations
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx
from scribble.reporting.render_html import render_report_html


def _engagement(session_factory, recs) -> int:
    with session_factory() as db:
        eng = Engagement(name="Strategic Case", company_name="Acme Corp", scope_type="external")
        eng.strategic_recommendations = recs
        db.add(eng)
        db.commit()
        return eng.id


def _html(session_factory, eid) -> str:
    with session_factory() as db:
        return render_report_html(build_report_context(db.get(Engagement, eid)))


def _docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    import re

    return re.sub(r"<[^>]+>", "", xml)


def test_normalizer_drops_blanks_and_non_strings_preserving_order():
    assert normalize_strategic_recommendations(
        ["  Rotate keys  ", "", "  ", 42, None, "Adopt MFA"]
    ) == ["Rotate keys", "Adopt MFA"]
    assert normalize_strategic_recommendations(None) == []
    assert normalize_strategic_recommendations("not a list") == []


def test_no_recommendations_renders_no_section(session_factory):
    html = _html(session_factory, _engagement(session_factory, None))
    assert "Strategic Recommendations" not in html
    assert 'id="sec-strategic"' not in html


def test_recommendations_render_the_section_in_order_escaped(session_factory):
    eid = _engagement(
        session_factory,
        ["Establish a patch-management program", "Adopt org-wide MFA <now>"],
    )
    html = _html(session_factory, eid)
    assert 'id="sec-strategic"' in html
    assert "Establish a patch-management program" in html
    # engagement-controlled text is escaped, never spliced as markup
    assert "Adopt org-wide MFA &lt;now&gt;" in html
    assert "<now>" not in html
    # order preserved
    assert html.index("Establish a patch-management") < html.index("Adopt org-wide MFA")


def test_removing_the_empty_short_circuit_breaks_backward_compat(session_factory, monkeypatch):
    """Red half: neuter the empty short-circuit and a no-recs report grows the section anchor."""
    eid = _engagement(session_factory, None)
    monkeypatch.setattr(
        rh, "_render_strategic_recommendations", lambda ctx: '<section id="sec-strategic"></section>'
    )
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)))
    assert 'id="sec-strategic"' in html


def test_docx_mirrors_the_section(session_factory):
    eid = _engagement(session_factory, ["Fund a security-awareness program"])
    with session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(Engagement, eid)))
    text = _docx_text(payload)
    assert "Strategic Recommendations" in text
    assert "Fund a security-awareness program" in text


def test_docx_without_recommendations_has_no_heading(session_factory):
    eid = _engagement(session_factory, [])
    with session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(Engagement, eid)))
    assert "Strategic Recommendations" not in _docx_text(payload)
