"""Report composition controls: per-section suppression + per-host suppression must reach every renderer.

The operator curates what a finding shows. Two mechanisms, one principle — the renderer does a best-effort
fill and skips what isn't there:

* per-section: ``BoardFinding.suppressed_sections`` omits named sections (content blocks + the derived
  affected-assets / evidence / references / reproduction sections). Honored once in
  ``build_report_context`` (the data is made absent), so all renderers skip it.
* per-host: a CHILD finding's ``include_in_report=False`` drops that host from the report (``report_visible``)
  while the board editor still lists it — suppress-but-keep-tracked.

These assert each section disappears ONLY when named, so a broken filter that drops everything (or nothing)
goes red.
"""
from __future__ import annotations

import uuid

from scribble import models as M
from scribble.content import schema
from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.reporting import build_report_context
from scribble.reporting.render_html import render_report_html

SECTIONS = ("description", "remediation", "details", "reproduction", "affected_assets",
            "evidence", "references")


def _build(session_factory, *, suppressed=(), child_included=True) -> tuple:
    with session_factory() as db:
        eng = M.ReportBoard(name="Supp Eng", company_name="Acme", scope_type="external")
        grp = M.FindingGroup(engagement=eng, name="Net", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        parent = M.BoardFinding(
            engagement_id=eng.id, group_id=grp.id, order_index=0, severity=Severity.high,
            title="Fleet vuln [cve-2021-33044]",
            content_json={
                "description": schema.doc_from_text("DESCBODY_marker"),
                "remediation": schema.doc_from_text("REMEDIATIONBODY_marker"),
                "details": schema.doc_from_text("DETAILSBODY_marker"),
            },
            references=[{"label": "REFLABEL_marker", "url": "http://ref.example/x", "source": "author"}],
            suppressed_sections=list(suppressed),
        )
        db.add(parent)
        db.flush()
        child = M.BoardFinding(
            engagement_id=eng.id, group_id=grp.id, order_index=1, severity=Severity.high,
            title="Fleet vuln [cve-2021-33044]", parent_id=parent.id,
            target_host="192.0.2.9", target_port="443",
            target_url="http://192.0.2.9/vulnpath?x=1", content_json={},
            include_in_report=child_included,
        )
        db.add(child)
        db.flush()
        db.add(M.Artifact(
            engagement_id=eng.id, finding_id=parent.id, kind=ArtifactKind.screenshot,
            placement=ArtifactPlacement.attached, filename="EVIDENCE_marker.png",
            content_type="image/png", storage_path=f"obj:{uuid.uuid7()}", byte_size=3,
        ))
        db.commit()
        return eng.id, parent.id, child.id


def _parent_ctx(session_factory, eng_id, parent_id):
    with session_factory() as db:
        ctx = build_report_context(db.get(M.ReportBoard, eng_id))
    for g in ctx.groups:
        for f in g.findings:
            if f.id == parent_id:
                return f
    raise AssertionError("parent finding not in report context")


def test_baseline_renders_every_section(app, session_factory):
    eng_id, parent_id, _ = _build(session_factory)
    with app.app_context():
        f = _parent_ctx(session_factory, eng_id, parent_id)
        html = render_report_html(_ctx_for(session_factory, eng_id))
    assert set(f.blocks_html) >= {"description", "remediation", "details"}
    assert f.references and f.artifacts                    # evidence + references present
    for marker in ("DESCBODY_marker", "REMEDIATIONBODY_marker", "DETAILSBODY_marker", "REFLABEL_marker"):
        assert marker in html
    assert "affected-assets" in html and "192.0.2.9" in html


def test_per_section_suppression_omits_only_named_sections(app, session_factory):
    supp = ["remediation", "details", "evidence", "references", "affected_assets", "reproduction"]
    eng_id, parent_id, _ = _build(session_factory, suppressed=supp)
    with app.app_context():
        f = _parent_ctx(session_factory, eng_id, parent_id)
        html = render_report_html(_ctx_for(session_factory, eng_id))
    # kept
    assert "description" in f.blocks_html and "DESCBODY_marker" in html
    # content blocks dropped from the ctx (the single propagation point)
    assert "remediation" not in f.blocks_html and "details" not in f.blocks_html
    assert "REMEDIATIONBODY_marker" not in html and "DETAILSBODY_marker" not in html
    # structural sections emptied in the ctx
    assert f.artifacts == [] and f.references == []
    assert "REFLABEL_marker" not in html
    # derived sections carried to the renderer via f.suppressed and skipped there
    assert f.suppressed.issuperset({"affected_assets", "reproduction"})
    assert "affected-assets" not in html
    assert "192.0.2.9" not in html          # host only ever appears via affected/repro, both suppressed
    # docx honors the same suppression (assets/repro nulled -> template `{% if %}` drops them): renders
    # without crashing on the emptied sections.
    from scribble.reporting.render_docx import render_report_docx
    assert render_report_docx(_ctx_for(session_factory, eng_id))[:2] == b"PK"


def test_per_host_suppression_drops_host_but_keeps_the_row(app, session_factory):
    eng_id, parent_id, child_id = _build(session_factory, child_included=False)
    with app.app_context():
        f = _parent_ctx(session_factory, eng_id, parent_id)
        html = render_report_html(_ctx_for(session_factory, eng_id))
    # the suppressed child host is gone from the report...
    assert all(c.target_host != "192.0.2.9" for c in f.children)
    assert "192.0.2.9" not in html
    # ...but the child row is still in the database, visible to the board editor for re-inclusion
    with session_factory() as db:
        assert db.get(M.BoardFinding, child_id) is not None


def _ctx_for(session_factory, eng_id):
    with session_factory() as db:
        return build_report_context(db.get(M.ReportBoard, eng_id))
