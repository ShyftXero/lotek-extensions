"""Editable + AI-rephrased standing prose (#Q4): per-report Methodology / Scope-and-limitations overrides.

An override REPLACES the generated standing text in BOTH deliverables; empty falls back to the standing
text. The save route stores it (empty => NULL/reset); the rephrase route rewrites a draft through the host
AI hook, and is 503 when the host provides none.
"""
from __future__ import annotations

import io
import uuid
import zipfile

import scribble.models as fm
from scribble.enums import Severity
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx
from scribble.reporting.render_html import render_report_html

API = "/scribble/api"
UI = "/scribble"


def _docx_text(payload: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml").decode("utf-8", "replace")


def _eng(session_factory, **cols) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name="Prose Eng", client_id=uuid.uuid7(), company_name="Acme", **cols)
        grp = fm.FindingGroup(engagement=eng, name="G", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        db.add(fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, order_index=0, title="V",
                               severity=Severity.high, content_json={}))
        db.commit()
        return eng.id


def test_default_prose_is_the_standing_text(session_factory):
    eng_id = _eng(session_factory)
    with session_factory() as db:
        ctx = build_report_context(db.get(fm.ReportBoard, eng_id))
        html = render_report_html(ctx, inline_assets=True)
        docx_text = _docx_text(render_report_docx(ctx))
    assert "phases below" in html            # the generated standing methodology lead
    assert "not proof that none exists" in html  # a standing scope/limitations statement
    assert "not proof that none exists" in docx_text  # DOCX now carries scope/limitations too (parity)


def test_overrides_replace_the_standing_text_in_both_renderers(session_factory):
    eng_id = _eng(session_factory,
                  methodology_text="We tested via carrier pigeon.\n\nThen we wrote it down.",
                  scope_limitations_text="Only the north wing was in scope.")
    with session_factory() as db:
        ctx = build_report_context(db.get(fm.ReportBoard, eng_id))
        html = render_report_html(ctx, inline_assets=True)
        docx_text = _docx_text(render_report_docx(ctx))
    for text in (html, docx_text):
        assert "carrier pigeon" in text
        assert "Only the north wing was in scope." in text
        assert "phases below" not in text                 # standing methodology replaced
        assert "not proof that none exists" not in text    # standing scope/limitations replaced


def test_save_prose_sets_and_resets(client, session_factory):
    eng_id = _eng(session_factory)
    r = client.post(f"{UI}/engagements/{eng_id}/report/prose",
                    data={"methodology_text": "Custom method.", "scope_limitations_text": "Custom scope."})
    assert r.status_code == 302
    with session_factory() as db:
        board = db.get(fm.ReportBoard, eng_id)
        assert board.methodology_text == "Custom method." and board.scope_limitations_text == "Custom scope."
    # empty fields RESET to the standing text (stored NULL)
    r2 = client.post(f"{UI}/engagements/{eng_id}/report/prose",
                     data={"methodology_text": "  ", "scope_limitations_text": ""})
    assert r2.status_code == 302
    with session_factory() as db:
        board = db.get(fm.ReportBoard, eng_id)
        assert board.methodology_text is None and board.scope_limitations_text is None


def test_rephrase_is_503_without_a_host_ai_hook(client):
    resp = client.post(f"{API}/report/prose/rephrase", json={"field": "methodology", "text": "hello"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "ai_unavailable"


def test_rephrase_uses_the_host_ai_hook(client, app):
    captured = {}

    def fake_ai(messages, **_kw):
        captured["messages"] = messages
        return "  A crisper rewrite.  "

    app.extensions["scribble"].extras["ai_complete"] = fake_ai
    try:
        resp = client.post(f"{API}/report/prose/rephrase",
                           json={"field": "scope", "text": "the original blurb"})
    finally:
        app.extensions["scribble"].extras.pop("ai_complete", None)
    assert resp.status_code == 200
    assert resp.get_json()["text"] == "A crisper rewrite."  # trimmed
    # the draft was actually sent to the hook
    assert any("the original blurb" in m.get("content", "") for m in captured["messages"])


def test_rephrase_failure_does_not_leak_the_host_ai_exception(client, app):
    """A raising AI hook must NOT surface its exception text to the client — the host AI client's error can
    carry provider URLs, model names or API-key fragments. The route returns a FIXED 502 message; the
    detail (which used to be ``str(exc)``) must not echo the exception."""
    secret = "https://api.provider.example/v1 key=sk-SECRET-DEADBEEF"

    def raising_ai(_messages, **_kw):
        raise RuntimeError(secret)

    app.extensions["scribble"].extras["ai_complete"] = raising_ai
    try:
        resp = client.post(f"{API}/report/prose/rephrase",
                           json={"field": "methodology", "text": "draft"})
    finally:
        app.extensions["scribble"].extras.pop("ai_complete", None)
    assert resp.status_code == 502
    body = resp.get_data(as_text=True)
    assert "SECRET" not in body and "sk-" not in body and secret not in body
    assert resp.get_json()["error"] == "ai_failed"
