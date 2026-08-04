"""Sprint 0 smoke tests: the app boots, seeds, pages render, and the report context honors the
grouping/ordering + include-exclude + variable-resolution contracts."""

from __future__ import annotations

from scribble.content import schema
from scribble.enums import OrderMode, Severity
from scribble.models import (
    AssessmentType,
    Client,
    Engagement,
    EngagementFinding,
    FindingGroup,
    VulnerabilityTemplate,
)
from scribble.reporting import build_report_context
from scribble.templating import BUILTIN_KEYS


def test_health(client):
    resp = client.get("/scribble/api/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["counts"]["templates"] > 0


def test_pages_render(client):
    for path in ("/scribble/", "/scribble/library", "/scribble/engagements"):
        assert client.get(path).status_code == 200


def test_seed(session_factory):
    with session_factory() as db:
        assert db.query(AssessmentType).count() == 4
        from scribble.models import TemplateVariable

        assert db.query(TemplateVariable).filter_by(builtin=True).count() == len(BUILTIN_KEYS)
        assert db.query(VulnerabilityTemplate).count() == 63  # 44 FACTION + 19 lotek entries


def test_client_normalized_in_seed(session_factory):
    # FACTION seed literal CLIENT should be normalized to {{COMPANY_NAME}} on import.
    with session_factory() as db:
        blob = " ".join(
            schema.plain_text(t.content_json.get("description"))
            for t in db.query(VulnerabilityTemplate).all()
        )
    assert "{{COMPANY_NAME}}" in blob


def _finding(engagement, group, title, sev, order, *, target_host=None, block=None):
    return EngagementFinding(
        engagement=engagement,
        group=group,
        title=title,
        severity=sev,
        order_index=order,
        target_host=target_host,
        content_json={"description": block} if block else {},
    )


def test_report_context_ordering_and_filtering(session_factory):
    with session_factory() as db:
        c = Client(name="Acme")
        db.add(c)
        db.flush()
        eng = Engagement(name="Q3 Combined", client_id=c.id, company_name="Acme Corp")
        internal = FindingGroup(engagement=eng, name="Internal", order_index=0)
        external = FindingGroup(engagement=eng, name="External", order_index=1)
        # auto_severity: low added first but critical must sort ahead of it.
        _finding(eng, internal, "int-low", Severity.low, 0)
        _finding(eng, internal, "int-crit", Severity.critical, 1)
        _finding(eng, external, "ext-med", Severity.medium, 0)
        excluded = _finding(eng, external, "ext-hidden", Severity.high, 1)
        excluded.include_in_report = False
        db.add(eng)
        db.commit()

        assert eng.resolve_client(db).name == "Acme"

        ctx = build_report_context(eng)

        assert [g.name for g in ctx.groups] == ["Internal", "External"]  # by order_index
        assert [f.title for f in ctx.groups[0].findings] == ["int-crit", "int-low"]  # worst-first
        assert [f.title for f in ctx.groups[1].findings] == ["ext-med"]  # excluded one filtered out
        assert ctx.rollup.overall == Severity.critical.value
        assert ctx.rollup.total == 3
        assert ctx.client_name == "Acme"


def test_report_context_manual_order(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Manual", company_name="Acme")
        g = FindingGroup(engagement=eng, name="External", order_index=0, order_mode=OrderMode.manual)
        _finding(eng, g, "second-added-first-shown", Severity.low, 0)
        _finding(eng, g, "first-added-second-shown", Severity.critical, 1)
        db.add(eng)
        db.commit()

        ctx = build_report_context(eng)
        # manual mode ignores severity: order_index wins.
        assert [f.title for f in ctx.groups[0].findings] == [
            "second-added-first-shown",
            "first-added-second-shown",
        ]


def test_report_context_nests_children_under_parent(session_factory):
    """D1 (nested render): a finding whose ``parent_id`` points at another finding in the SAME
    ordered list becomes that parent's ``FindingCtx.children`` instead of its own top-level entry."""
    with session_factory() as db:
        eng = Engagement(name="Nested", company_name="Acme")
        g = FindingGroup(engagement=eng, name="Internal", order_index=0)
        parent = _finding(
            eng, g, "Weak SMB Signing", Severity.high, 0, target_host="10.0.0.1"
        )
        db.add(eng)
        db.flush()  # assign ids so the children below can reference parent.id

        child_a = _finding(eng, g, "Weak SMB Signing", Severity.high, 1, target_host="10.0.0.2")
        child_b = _finding(eng, g, "Weak SMB Signing", Severity.high, 2, target_host="10.0.0.3")
        child_a.parent_id = parent.id
        child_b.parent_id = parent.id
        db.add_all([child_a, child_b])
        db.commit()

        ctx = build_report_context(db.get(Engagement, eng.id))

    findings = ctx.groups[0].findings
    # Only the parent is top-level -- children never get their own top-level FindingCtx entry.
    assert [f.title for f in findings] == ["Weak SMB Signing"]
    assert len(findings) == 1
    parent_ctx = findings[0]
    assert len(parent_ctx.children) == 2
    assert {c.target_host for c in parent_ctx.children} == {"10.0.0.2", "10.0.0.3"}


def test_report_context_childless_finding_has_empty_children(session_factory):
    """A finding with no children renders exactly as before nesting existed: ``children == []``."""
    with session_factory() as db:
        eng = Engagement(name="No Nesting", company_name="Acme")
        g = FindingGroup(engagement=eng, name="Internal", order_index=0)
        _finding(eng, g, "Standalone Finding", Severity.medium, 0)
        db.add(eng)
        db.commit()

        ctx = build_report_context(db.get(Engagement, eng.id))

    assert ctx.groups[0].findings[0].children == []


def test_report_context_orphaned_child_falls_back_to_top_level(session_factory):
    """A finding whose ``parent_id`` doesn't resolve within the same ordered list (parent excluded from
    the report here; missing/cross-group are the same code path) falls back to rendering top-level
    rather than disappearing. ``parent_id`` is a real FK, so the "missing parent" case exercised here is
    a real finding row that ``_order_findings`` has already filtered out -- not a dangling id, which
    the FK constraint wouldn't allow to be inserted at all."""
    with session_factory() as db:
        eng = Engagement(name="Orphan", company_name="Acme")
        g = FindingGroup(engagement=eng, name="Internal", order_index=0)
        excluded_parent = _finding(eng, g, "Excluded Parent", Severity.low, 0)
        excluded_parent.include_in_report = False
        db.add(eng)
        db.flush()

        orphan = _finding(eng, g, "Orphaned Child", Severity.low, 1)
        orphan.parent_id = excluded_parent.id
        db.add(orphan)
        db.commit()

        ctx = build_report_context(db.get(Engagement, eng.id))

    findings = ctx.groups[0].findings
    assert [f.title for f in findings] == ["Orphaned Child"]
    assert findings[0].children == []


def test_report_context_narrative_is_populated_and_data_derived(session_factory):
    """D2: ``ReportContext.narrative`` is synthesized from the rollup + worst finding titles, not
    hand-authored -- non-empty whenever there are findings, and names the top-severity finding."""
    with session_factory() as db:
        c = Client(name="Acme")
        db.add(c)
        db.flush()
        eng = Engagement(name="Narrative Co", client_id=c.id, company_name="Acme Corp")
        g = FindingGroup(engagement=eng, name="Internal", order_index=0)
        _finding(eng, g, "Domain Admin Compromise", Severity.critical, 0)
        _finding(eng, g, "Weak SMB Signing", Severity.low, 1)
        db.add(eng)
        db.commit()

        ctx = build_report_context(db.get(Engagement, eng.id))

    assert ctx.narrative != ""
    assert "Acme Corp" in ctx.narrative
    assert "Domain Admin Compromise" in ctx.narrative
    assert "2" in ctx.narrative  # total finding count is factual, not decorative


def test_report_context_narrative_empty_engagement(session_factory):
    """A clean engagement (no findings) still gets a factual, non-empty narrative sentence."""
    with session_factory() as db:
        eng = Engagement(name="Clean", company_name="Acme")
        db.add(eng)
        db.commit()

        ctx = build_report_context(db.get(Engagement, eng.id))

    assert ctx.narrative != ""
    assert "Acme" in ctx.narrative


def test_variable_resolution_in_context(session_factory):
    block = {
        "type": schema.DOC,
        "content": [
            {
                "type": schema.PARAGRAPH,
                "content": [
                    {"type": schema.TEXT, "text": "Affected host: "},
                    {"type": schema.VARIABLE, "attrs": {"key": "TARGET_HOST"}},
                    {"type": schema.TEXT, "text": " for {{COMPANY_NAME}}."},
                ],
            }
        ],
    }
    with session_factory() as db:
        eng = Engagement(name="Vars", company_name="Acme Corp")
        g = FindingGroup(engagement=eng, name="Web App", order_index=0)
        _finding(eng, g, "xss", Severity.high, 0, target_host="10.0.0.5", block=block)
        db.add(eng)
        db.commit()

        ctx = build_report_context(eng)
        html = ctx.groups[0].findings[0].blocks_html["description"]
    assert "10.0.0.5" in html
    assert "Acme Corp" in html
    assert "{{" not in html


def test_from_template(session_factory):
    with session_factory() as db:
        tmpl = db.query(VulnerabilityTemplate).first()
        finding = EngagementFinding.from_template(tmpl)
        assert finding.title == tmpl.name
        assert finding.template_id == tmpl.id
        assert finding.severity == tmpl.default_severity
