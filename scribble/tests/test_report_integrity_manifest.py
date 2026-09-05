"""#626 — evidence-integrity SHA-256 manifest.

Every uploaded artifact already carries a content hash (``Artifact.sha256``, stamped at persist time —
see ``api_pat`` upload path). This pins that the hash reaches the two surfaces a client verifies against:

- the report's Evidence appendix publishes a filename → SHA-256 manifest table (HTML + docx), so a
  recipient can confirm the delivered files were not altered; and
- the machine artifact surface (``_machine_artifact_dict`` / ``_artifact_summary``) exposes the same
  hash (feeds #627).

No migration: the column exists; this only carries it through ``ArtifactCtx`` and the two renderers.
"""

from __future__ import annotations

import io

import docx
from docx.oxml.ns import qn

from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.models import Artifact, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx
from scribble.reporting.render_html import render_report_html

# sha256("") — a real, recognisable 64-hex digest so an assertion is checking the value, not a shape.
KNOWN_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _engagement_with_hashed_evidence(session_factory) -> int:
    with session_factory() as db:
        eng = Engagement(name="Integrity Eng", company_name="Acme")
        group = FindingGroup(engagement=eng, name="Web App", order_index=0)
        EngagementFinding(
            engagement=eng, group=group, title="A finding", severity=Severity.medium,
            order_index=0, content_json={},
        )
        db.add(eng)
        db.flush()
        db.add(Artifact(
            engagement=eng, finding_id=None, kind=ArtifactKind.screenshot,
            placement=ArtifactPlacement.attached, filename="proof.png",
            content_type="image/png", storage_path="proof.png", caption="Proof",
            order_index=0, sha256=KNOWN_SHA,
        ))
        db.commit()
        return eng.id


def test_artifact_ctx_carries_the_sha256(session_factory):
    eid = _engagement_with_hashed_evidence(session_factory)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    assert ctx.artifacts[0].sha256 == KNOWN_SHA


def test_html_evidence_appendix_renders_the_integrity_manifest(session_factory):
    eid = _engagement_with_hashed_evidence(session_factory)
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)))
    assert KNOWN_SHA in html, "the SHA-256 manifest is missing from the report"
    assert "proof.png" in html
    assert "integrity" in html.lower(), "manifest should be labelled, not just a bare hash in a caption"


def test_docx_evidence_appendix_lists_the_sha256(session_factory):
    eid = _engagement_with_hashed_evidence(session_factory)
    with session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(Engagement, eid)))
    text = "".join(
        t.text or "" for t in docx.Document(io.BytesIO(payload)).element.body.iter(qn("w:t"))
    )
    assert KNOWN_SHA in text, "the SHA-256 hash is missing from the docx Evidence Appendix"


def test_machine_artifact_surfaces_include_sha256(app, session_factory):
    from scribble.api_pat import _artifact_summary, _machine_artifact_dict

    eid = _engagement_with_hashed_evidence(session_factory)
    with session_factory() as db, app.test_request_context():
        artifact = db.get(Engagement, eid).artifacts[0]
        assert _machine_artifact_dict(artifact)["sha256"] == KNOWN_SHA
        assert _artifact_summary(artifact)["sha256"] == KNOWN_SHA
