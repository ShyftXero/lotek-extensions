"""The optional engagement Activity Log appendix (lotek#442).

An OPT-IN report appendix — included by putting the ``activity_log`` block in a Layout (the
"checkbox") — that renders a timestamped trail of engagement activity (finding added, evidence
uploaded, diagram created) built from scribble's OWN ``created_at`` columns, with its own TOC entry.
These pin: the trail is built chronologically from real timestamps, the appendix + its TOC entry appear
only when the block is opted in AND there is activity, and it escapes engagement-controlled text.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.models import Artifact, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.layouts import ReportLayout
from scribble.reporting.render_html import (
    _AssetResolver,
    _render_activity_appendix,
    _render_document,
    _toc_entries,
)

# A Layout that opts the appendix in (the "checkbox"), and one that does not.
_WITH_ACTIVITY = ReportLayout(
    "with-activity", "With activity", ("cover", "toc", "findings", "activity_log")
)
_WITHOUT_ACTIVITY = ReportLayout("no-activity", "No activity", ("cover", "toc", "findings"))


def _engagement_with_activity(session_factory) -> int:
    with session_factory() as db:
        eng = Engagement(name="Activity Co Assessment")
        grp = FindingGroup(engagement=eng, name="Findings", order_index=0)
        EngagementFinding(engagement=eng, group=grp, title="SMB signing not required",
                          severity=Severity.medium, order_index=0)
        EngagementFinding(engagement=eng, group=grp, title="Anonymous FTP enabled",
                          severity=Severity.low, order_index=1)
        db.add_all([eng, grp])
        db.flush()
        db.add(Artifact(engagement=eng, finding=None, kind=ArtifactKind.screenshot,
                        placement=ArtifactPlacement.attached, filename="capture.png",
                        content_type="image/png", storage_path="capture.png",
                        caption="evidence", order_index=0))
        db.commit()
        return eng.id


def _toc_targets(html: str) -> list[str]:
    m = re.search(r'<nav class="toc".*?</nav>', html, re.S)
    return re.findall(r'href="#([^"]+)"', m.group(0)) if m else []


def test_activity_log_built_from_engagement_timestamps(session_factory):
    eid = _engagement_with_activity(session_factory)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    summaries = [e.summary for e in ctx.activity_log]
    assert "Engagement created: Activity Co Assessment" in summaries
    assert "Finding added: SMB signing not required" in summaries
    assert "Finding added: Anonymous FTP enabled" in summaries
    assert "Evidence uploaded: capture.png" in summaries
    # chronological: engagement creation is the earliest event, so it sorts first
    assert ctx.activity_log[0].summary.startswith("Engagement created")
    # every row carries a kind + a rendered timestamp string
    assert {e.kind for e in ctx.activity_log} <= {"engagement", "finding", "evidence", "diagram"}
    assert all(e.timestamp for e in ctx.activity_log)


def test_appendix_and_toc_entry_appear_only_when_opted_in(session_factory):
    eid = _engagement_with_activity(session_factory)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    with_html = _render_document(ctx, _AssetResolver("none", None), layout=_WITH_ACTIVITY)
    assert 'id="sec-activity_log"' in with_html
    assert "Activity Log" in with_html
    assert "sec-activity_log" in _toc_targets(with_html)  # gets its own TOC entry
    assert "Finding added: SMB signing not required" in with_html
    # opt-out: no block -> no section, no contents entry
    without_html = _render_document(ctx, _AssetResolver("none", None), layout=_WITHOUT_ACTIVITY)
    assert 'id="sec-activity_log"' not in without_html
    assert "sec-activity_log" not in _toc_targets(without_html)


def test_empty_activity_log_omits_the_appendix_and_its_toc_entry():
    """Like the Evidence appendix: an empty trail renders nothing and registers no TOC entry, so a
    Layout that opts the block in for an engagement with no activity stays clean."""
    empty = SimpleNamespace(activity_log=[], groups=[], diagrams=[], artifacts=[], checklists=[])
    assert _render_activity_appendix(empty) == ""
    entries = _toc_entries(empty, ("activity_log",))
    assert entries == []


def test_summaries_are_escaped(session_factory):
    """Finding titles / filenames are engagement-controlled and land in the appendix, so they must be
    HTML-escaped — a title with a tag must not inject markup."""
    with session_factory() as db:
        eng = Engagement(name="XSS Co")
        grp = FindingGroup(engagement=eng, name="Findings", order_index=0)
        EngagementFinding(engagement=eng, group=grp, title="<script>alert(1)</script>",
                          severity=Severity.high, order_index=0)
        db.add_all([eng, grp])
        db.commit()
        eid = eng.id
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    html = _render_document(ctx, _AssetResolver("none", None), layout=_WITH_ACTIVITY)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
