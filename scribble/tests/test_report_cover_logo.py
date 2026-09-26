"""Cover logo (#Q3): the cover shows the stock lotek mark by default; a picked library logo
(ScribbleReportLogo) overrides it, in BOTH deliverables; a deleted pick falls back to the default. One
resolver (reporting.logos.resolve_cover_logo) feeds both renderers via ReportContext.cover_logo.
"""
from __future__ import annotations

import base64
import io
import uuid

import scribble.models as fm
from scribble.enums import Severity
from scribble.reporting import build_report_context
from scribble.reporting.logos import default_logo_bytes
from scribble.reporting.render_docx import render_report_docx
from scribble.reporting.render_html import render_report_html

_PNG = bytes.fromhex(  # a real 1x1 PNG (valid dimensions, so docxtpl InlineImage can embed it)
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360606000000005000166ff0f0e0000000049454e44ae426082"
)


def _pictures(payload: bytes) -> int:
    import docx
    from docx.oxml.ns import qn
    d = docx.Document(io.BytesIO(payload))
    return len(d.element.body.findall(".//" + qn("pic:pic")))


def _eng(session_factory, *, logo_id=None) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name="Cover Eng", client_id=uuid.uuid7(), company_name="Acme",
                             cover_logo_id=logo_id)
        grp = fm.FindingGroup(engagement=eng, name="G", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        db.add(fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, order_index=0, title="V",
                               severity=Severity.high, content_json={}))
        db.commit()
        return eng.id


def test_cover_defaults_to_the_stock_lotek_mark(session_factory):
    eng_id = _eng(session_factory)
    with session_factory() as db:
        ctx = build_report_context(db.get(fm.ReportBoard, eng_id))
        assert ctx.cover_logo == default_logo_bytes()  # the stock mark, no pick
        html = render_report_html(ctx, inline_assets=True)
        docx_bytes = render_report_docx(ctx)
    assert 'class="cover-logo"' in html and "data:image/png;base64," in html
    assert _pictures(docx_bytes) >= 1  # the mark is embedded on the cover


def test_picked_library_logo_overrides_the_default(session_factory):
    with session_factory() as db:
        logo = fm.ScribbleReportLogo(label="Acme brand", content_type="image/png", data=_PNG,
                                     created_by="op")
        db.add(logo)
        db.commit()
        logo_id = logo.id
    eng_id = _eng(session_factory, logo_id=logo_id)
    with session_factory() as db:
        ctx = build_report_context(db.get(fm.ReportBoard, eng_id))
        assert ctx.cover_logo == _PNG  # the PICKED bytes, not the stock mark
        assert ctx.cover_logo != default_logo_bytes()
        html = render_report_html(ctx, inline_assets=True)
    assert base64.b64encode(_PNG).decode("ascii") in html  # the picked logo is what the cover embeds


def test_deleting_a_library_logo_nulls_the_pick_and_falls_back(session_factory):
    # ON DELETE SET NULL: removing a library logo drops every report's pick back to the stock mark rather
    # than dangling — so a report never crashes or 404s its cover because a logo was cleaned up.
    with session_factory() as db:
        logo = fm.ScribbleReportLogo(label="temp", content_type="image/png", data=_PNG)
        db.add(logo)
        db.commit()
        logo_id = logo.id
    eng_id = _eng(session_factory, logo_id=logo_id)
    with session_factory() as db:
        db.delete(db.get(fm.ScribbleReportLogo, logo_id))
        db.commit()
    with session_factory() as db:
        board = db.get(fm.ReportBoard, eng_id)
        assert board.cover_logo_id is None  # the FK's ON DELETE SET NULL fired
        assert build_report_context(board).cover_logo == default_logo_bytes()


# --- the picker UI + its upload/serve/set routes -------------------------------------------------------

UI = "/scribble"


def test_engagement_page_renders_the_logo_picker(client, session_factory):
    eng_id = _eng(session_factory)
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    assert "scribble-cover-logo" in html
    assert "Default (lotek)" in html
    assert "/report/logos/default/raw" in html


def test_upload_adds_to_library_and_uses_it(client, session_factory):
    eng_id = _eng(session_factory)
    resp = client.post(f"{UI}/report/logos", data={
        "engagement_id": str(eng_id), "label": "Acme brand",
        "file": (io.BytesIO(_PNG), "acme.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    with session_factory() as db:
        logos = db.query(fm.ScribbleReportLogo).all()
        assert len(logos) == 1 and logos[0].label == "Acme brand"
        assert db.get(fm.ReportBoard, eng_id).cover_logo_id == logos[0].id  # upload-and-use


def test_upload_rejects_non_image(client):
    resp = client.post(f"{UI}/report/logos", data={"file": (io.BytesIO(b"not an image"), "x.txt")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def test_serve_library_logo_raw_is_inline_nosniff(client, session_factory):
    with session_factory() as db:
        logo = fm.ScribbleReportLogo(label="l", content_type="image/png", data=_PNG)
        db.add(logo)
        db.commit()
        lid = logo.id
    resp = client.get(f"{UI}/report/logos/{lid}/raw")
    assert resp.status_code == 200
    assert resp.data == _PNG
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_serve_default_logo_raw(client):
    resp = client.get(f"{UI}/report/logos/default/raw")
    assert resp.status_code == 200
    assert resp.data == default_logo_bytes()


def test_set_and_clear_report_logo(client, session_factory):
    with session_factory() as db:
        logo = fm.ScribbleReportLogo(label="l", content_type="image/png", data=_PNG)
        db.add(logo)
        db.commit()
        lid = logo.id
    eng_id = _eng(session_factory)
    pick = client.post(f"{UI}/engagements/{eng_id}/report/logo", data={"logo_id": str(lid)})
    assert pick.status_code == 302
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eng_id).cover_logo_id == lid
    clear = client.post(f"{UI}/engagements/{eng_id}/report/logo", data={"logo_id": "default"})
    assert clear.status_code == 302
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eng_id).cover_logo_id is None
