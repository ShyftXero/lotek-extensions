"""WS7: self-contained HTML report renderer tests.

Builds an engagement with two groups + findings of varied severities (one excluded, one whose
description carries a {{TARGET_HOST}} variable), then asserts the rendered document honors the
frozen contracts: group order, worst-first finding order, include/exclude filtering, severity
styling, the risk banner, variable resolution, and that the whole thing is one self-contained
document (no external stylesheet/script hosts).
"""

from __future__ import annotations

import io
import zipfile

from fraction.content import schema
from fraction.enums import ArtifactKind, ArtifactPlacement, Severity
from fraction.models import Artifact, Client, Engagement, EngagementFinding, FindingGroup
from fraction.reporting import build_report_context
from fraction.reporting.render_html import export_zip, render_report_html


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _target_host_block() -> dict:
    return {
        "type": schema.DOC,
        "content": [
            {
                "type": schema.PARAGRAPH,
                "content": [
                    {"type": schema.TEXT, "text": "Affected host: "},
                    {"type": schema.VARIABLE, "attrs": {"key": "TARGET_HOST"}},
                ],
            }
        ],
    }


def _build_engagement(session_factory) -> int:
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = Engagement(name="Q3 Combined Assessment", client_id=client.id, company_name="Acme Corp")
        internal = FindingGroup(engagement=eng, name="Internal", order_index=0)
        external = FindingGroup(engagement=eng, name="External", order_index=1)

        EngagementFinding(
            engagement=eng,
            group=internal,
            title="Weak SMB Signing",
            severity=Severity.low,
            order_index=0,
            content_json={"description": _block("SMB signing is not required on several hosts.")},
        )
        EngagementFinding(
            engagement=eng,
            group=internal,
            title="Domain Admin Compromise",
            severity=Severity.critical,
            order_index=1,
            content_json={"description": _block("Full domain compromise was achieved via Kerberoasting.")},
        )
        EngagementFinding(
            engagement=eng,
            group=external,
            title="Reflected XSS",
            severity=Severity.medium,
            order_index=0,
            target_host="app.acme.test",
            target_port="443",
            content_json={"description": _target_host_block()},
        )
        hidden = EngagementFinding(
            engagement=eng,
            group=external,
            title="Excluded Finding",
            severity=Severity.high,
            order_index=1,
            content_json={"description": _block("Should never render.")},
        )
        hidden.include_in_report = False

        db.add(eng)
        db.commit()
        return eng.id


def test_render_report_html_contract(session_factory):
    eng_id = _build_engagement(session_factory)
    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        html_doc = render_report_html(ctx)

    # --- self-contained single document -----------------------------------------------------
    assert html_doc.strip().startswith("<!doctype html>")
    assert "<style>" in html_doc and "</style>" in html_doc
    assert "<script>" in html_doc and "</script>" in html_doc
    assert '<link rel="stylesheet"' not in html_doc  # no external CSS
    assert 'src="http' not in html_doc  # no externally-hosted script/image

    # --- group order: Internal (order_index=0) before External (order_index=1) -------------
    idx_internal = html_doc.index("Internal")
    idx_external = html_doc.index("External")
    assert idx_internal < idx_external

    # --- finding order within a group: worst-first (auto_severity default) -----------------
    idx_crit = html_doc.index("Domain Admin Compromise")
    idx_low = html_doc.index("Weak SMB Signing")
    assert idx_crit < idx_low

    # --- excluded finding never renders ------------------------------------------------------
    assert "Excluded Finding" not in html_doc

    # --- severity classes present -------------------------------------------------------------
    assert "sev-critical" in html_doc
    assert "sev-medium" in html_doc
    assert "sev-low" in html_doc

    # --- risk banner reflects the worst included finding (critical) -------------------------
    assert "risk-critical" in html_doc

    # --- {{TARGET_HOST}} resolved, no raw mustache survives ----------------------------------
    assert "app.acme.test" in html_doc
    assert "{{" not in html_doc
    assert "}}" not in html_doc


def test_render_report_html_empty_engagement(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Clean Sweep", company_name="Acme")
        db.add(eng)
        db.commit()
        eng_id = eng.id

    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        html_doc = render_report_html(ctx)

    assert "No findings recorded" in html_doc
    assert "risk-clean" in html_doc


def test_inline_assets_embeds_data_uri(session_factory):
    with session_factory() as db:
        client = Client(name="Acme")
        db.add(client)
        db.flush()
        eng = Engagement(name="Assets Engagement", client_id=client.id, company_name="Acme")
        group = FindingGroup(engagement=eng, name="Web App", order_index=0)
        finding = EngagementFinding(
            engagement=eng,
            group=group,
            title="Stored XSS",
            severity=Severity.high,
            order_index=0,
            content_json={"description": _block("See evidence below.")},
        )
        db.add(eng)
        db.flush()
        artifact = Artifact(
            engagement=eng,
            finding=finding,
            kind=ArtifactKind.screenshot,
            placement=ArtifactPlacement.attached,
            filename="poc.png",
            content_type="image/png",
            storage_path="poc.png",
            caption="Proof of concept",
            order_index=0,
        )
        db.add(artifact)
        db.commit()
        eng_id = eng.id

    fake_files = {"poc.png": b"\x89PNG\r\n\x1a\nFAKEDATA"}

    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        embedded = render_report_html(ctx, inline_assets=True, artifact_bytes=fake_files.get)
        not_embedded = render_report_html(ctx)  # inline_assets defaults to False

    assert "data:image/png;base64," in embedded
    assert "Proof of concept" in embedded
    assert "data:image/png;base64," not in not_embedded
    assert "not embedded" in not_embedded


def test_render_report_html_renders_nested_children_compactly(session_factory):
    """D1: a parent finding's vuln-DB write-up renders ONCE; its children render as a compact
    per-host list (not one full flat card per instance). A child never gets its own top-level
    ``<article id="finding-N">`` card."""
    with session_factory() as db:
        eng = Engagement(name="Nested Report", company_name="Acme")
        g = FindingGroup(engagement=eng, name="Internal", order_index=0)
        parent = EngagementFinding(
            engagement=eng,
            group=g,
            title="Kerberoastable Account",
            severity=Severity.high,
            order_index=0,
            content_json={"description": _block("Kerberoastable accounts were identified.")},
        )
        db.add(eng)
        db.flush()

        child_a = EngagementFinding(
            engagement=eng,
            group=g,
            title="Kerberoastable Account",
            severity=Severity.high,
            order_index=1,
            target_host="dc01.acme.test",
            # Every child promoted under the same vuln-DB template shares that template's
            # ``content_json`` verbatim with its parent (see ``fraction.promote.promote_job``) -- these
            # per-child blocks are deliberately DIFFERENT text here to prove the renderer does NOT read
            # them for the per-host row; the real per-host evidence comes from ``variables`` instead
            # (``EngagementFinding.variables``, filled by promote from the host's own facts).
            content_json={"description": _block("Should not render as its own card.")},
            variables={"AFFECTED": "svc_sql"},
        )
        child_b = EngagementFinding(
            engagement=eng,
            group=g,
            title="Kerberoastable Account",
            severity=Severity.high,
            order_index=2,
            target_host="dc02.acme.test",
            content_json={"description": _block("Nor should this one.")},
            variables={"AFFECTED": "svc_web"},
        )
        child_a.parent_id = parent.id
        child_b.parent_id = parent.id
        db.add_all([child_a, child_b])
        db.commit()
        eng_id, parent_id, child_a_id, child_b_id = eng.id, parent.id, child_a.id, child_b.id

    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        html_doc = render_report_html(ctx)

    # Exactly ONE top-level finding card exists (the parent's) -- children never get their own.
    assert html_doc.count('<article class="finding ') == 1
    assert html_doc.count(f'id="finding-{parent_id}"') == 1
    assert "Kerberoastable accounts were identified." in html_doc

    # Neither child gets its own top-level card...
    assert f'id="finding-{child_a_id}"' not in html_doc
    assert f'id="finding-{child_b_id}"' not in html_doc
    # ...but each child's OWN facts-derived evidence line DOES appear, compactly, inside the children
    # table (not a full flat card, and not a copy of any content block) -- this is the "host + real
    # per-host evidence" list the spec calls for.
    assert '<table class="children-table">' in html_doc
    idx_children_table = html_doc.index('<table class="children-table">')
    idx_evidence_a = html_doc.index("svc_sql")
    idx_evidence_b = html_doc.index("svc_web")
    assert idx_children_table < idx_evidence_a
    assert idx_children_table < idx_evidence_b
    # Neither child's own content block is rendered anywhere -- confirming the evidence line comes
    # from ``variables``, not from a truncated copy of (parent or child) descriptive text.
    assert "Should not render as its own card." not in html_doc
    assert "Nor should this one." not in html_doc

    # The compact per-host list is present, naming both children's hosts.
    assert "Affected hosts (2)" in html_doc
    assert "dc01.acme.test" in html_doc
    assert "dc02.acme.test" in html_doc
    assert '<details class="children">' in html_doc


def test_render_report_html_childless_finding_unaffected(session_factory):
    """A finding with no children renders exactly as it did before nesting existed (no children
    block/markup at all)."""
    eng_id = _build_engagement(session_factory)
    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        html_doc = render_report_html(ctx)

    assert '<details class="children">' not in html_doc
    assert "Affected hosts" not in html_doc


def test_render_report_html_includes_narrative(session_factory):
    """D2: the generated executive-summary narrative renders inside the Executive Summary section."""
    eng_id = _build_engagement(session_factory)
    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        html_doc = render_report_html(ctx)

    assert ctx.narrative != ""
    assert 'class="summary-narrative"' in html_doc
    assert ctx.narrative in html_doc
    # It renders inside the Executive Summary section, before any finding card.
    assert html_doc.index(ctx.narrative) < html_doc.index('class="finding ')


def test_export_zip_bundles_report_and_artifacts(session_factory):
    eng_id = _build_engagement(session_factory)
    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        payload = export_zip(ctx, artifact_bytes=None)

    zf = zipfile.ZipFile(io.BytesIO(payload))
    names = zf.namelist()
    assert "report.html" in names
    report_html = zf.read("report.html").decode("utf-8")
    assert "Domain Admin Compromise" in report_html
    assert "Excluded Finding" not in report_html
