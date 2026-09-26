"""Render-time report font selection: the operator picks a body + code face and the docx bakes them in
(reporting.fonts.remap_fonts swaps the template's baked defaults across styles + runs). An unknown/None
choice keeps the default; the choosable set is what the lotek-gotenberg image actually has.
"""
from __future__ import annotations

import io
import uuid
import zipfile

import scribble.models as fm
from scribble.enums import Severity
from scribble.reporting import build_report_context
from scribble.reporting.fonts import (
    DEFAULT_BODY_FONT,
    DEFAULT_CODE_FONT,
    remap_fonts,
    valid_body_font,
    valid_code_font,
)
from scribble.reporting.render_docx import render_report_docx


def _engagement(session_factory) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name="Font Eng", client_id=uuid.uuid7())
        grp = fm.FindingGroup(engagement=eng, name="G", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        code_doc = {"type": "doc", "content": [
            {"type": "codeBlock", "content": [{"type": "text", "text": "GET / HTTP/1.1"}]}]}
        db.add(fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, order_index=0, title="V",
                               severity=Severity.high, content_json={"details": code_doc}))
        db.commit()
        return eng.id


def _fonts_in(payload: bytes) -> set[str]:
    zf = zipfile.ZipFile(io.BytesIO(payload))
    import re
    families: set[str] = set()
    for part in ("word/styles.xml", "word/document.xml"):
        xml = zf.read(part).decode("utf-8", "replace")
        families |= set(re.findall(r'w:ascii="([^"]+)"', xml))
    return families


def test_default_render_uses_the_baked_faces(app, session_factory):
    eng_id = _engagement(session_factory)
    with app.app_context(), session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(fm.ReportBoard, eng_id)))
    fonts = _fonts_in(payload)
    assert DEFAULT_BODY_FONT in fonts and DEFAULT_CODE_FONT in fonts


def test_chosen_body_and_code_fonts_replace_the_defaults(app, session_factory):
    eng_id = _engagement(session_factory)
    with app.app_context(), session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(fm.ReportBoard, eng_id)),
                                     body_font="Liberation Sans", code_font="DejaVu Sans Mono")
    fonts = _fonts_in(payload)
    assert "Liberation Sans" in fonts and "DejaVu Sans Mono" in fonts
    assert DEFAULT_BODY_FONT not in fonts, "the baked body face must be fully swapped out"
    assert DEFAULT_CODE_FONT not in fonts, "the baked code face must be fully swapped out"


def test_unknown_font_is_ignored_keeps_default(app, session_factory):
    eng_id = _engagement(session_factory)
    with app.app_context(), session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(fm.ReportBoard, eng_id)),
                                     body_font="Comic Sans MS")  # not in the image / menu
    fonts = _fonts_in(payload)
    assert DEFAULT_BODY_FONT in fonts and "Comic Sans MS" not in fonts


def test_validators_gate_the_menu():
    assert valid_body_font("Inter") == "Inter"
    assert valid_code_font("JetBrains Mono") == "JetBrains Mono"
    assert valid_body_font("Comic Sans MS") is None
    assert valid_code_font("") is None
    assert valid_body_font(None) is None


def test_fonts_from_settings_reads_and_validates():
    from types import SimpleNamespace

    from scribble.reporting.fonts import fonts_from_settings

    # A row with valid faces passes them through.
    ok = SimpleNamespace(report_body_font="Inter", report_code_font="JetBrains Mono")
    assert fonts_from_settings(ok) == ("Inter", "JetBrains Mono")
    # Unknown / None / a missing row all degrade to the baked default (None, None).
    bad = SimpleNamespace(report_body_font="Comic Sans MS", report_code_font=None)
    assert fonts_from_settings(bad) == (None, None)
    assert fonts_from_settings(None) == (None, None)


def test_remap_is_a_noop_without_changes():
    # A doc with no matching families is untouched; guards the "nothing maps -> return" path.
    import docx
    d = docx.Document()
    d.add_paragraph("x")
    remap_fonts(d, body_font=None, code_font=None)  # no error, no-op


# --- the font-selection GUI: settings route + picker + the docx route bakes the saved fonts (Task #9) ----

UI = "/scribble"


def _set_default_settings_fonts(session_factory, *, body=None, code=None) -> None:
    with session_factory() as db:
        s = fm.ScribbleSettings(slot="default", report_body_font=body, report_code_font=code)
        db.add(s)
        db.commit()


def test_save_report_fonts_route_persists_and_validates(client, session_factory):
    resp = client.post(f"{UI}/settings/report-fonts",
                       data={"body_font": "Liberation Sans", "code_font": "Comic Sans MS"})
    assert resp.status_code == 302
    with session_factory() as db:
        s = db.query(fm.ScribbleSettings).filter_by(slot="default").one()
        assert s.report_body_font == "Liberation Sans"   # valid -> stored
        assert s.report_code_font is None                # unknown code face -> NULL (baked default)


def test_themes_page_renders_the_font_picker(client):
    html = client.get(f"{UI}/themes").get_data(as_text=True)
    assert "Report fonts" in html
    assert 'name="body_font"' in html and 'name="code_font"' in html
    assert "Liberation Sans" in html  # a body choice is offered


def test_docx_route_bakes_the_saved_fonts(client, session_factory):
    eng_id = _engagement(session_factory)
    _set_default_settings_fonts(session_factory, body="Liberation Sans", code="DejaVu Sans Mono")
    resp = client.get(f"{UI}/engagements/{eng_id}/report.docx")
    assert resp.status_code == 200
    fonts = _fonts_in(resp.data)
    assert "Liberation Sans" in fonts and "DejaVu Sans Mono" in fonts
    assert DEFAULT_BODY_FONT not in fonts  # the install setting swapped the baked default out
