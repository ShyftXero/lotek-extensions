"""WS3 tests: engagement CRUD, group/board management, and the two-level drag-and-drop finding board.

`scribble.engagement_ui.register(api_bp, bp)` is already wired unconditionally by
`scribble/__init__.py:_wire_feature_routes`, so the plain `app`/`client`/`session_factory` fixtures from
`tests/conftest.py` are enough here -- unlike the Sprint-1 WS4/WS5 test modules (which predated that
integration and had to self-wire their hooks before building the test app).

Coverage: engagement create (select-or-create Client) + list + board detail; group CRUD; add-finding-
from-template into named groups; group reorder (persists, and is defensive against stale/foreign/missing
ids); finding move across groups (updates group_id + reindexes both the source and destination groups
with no gaps); the auto_severity -> manual flip on the first manual drag (and that a "re-rank by
severity" call resets it); and an end-to-end assertion that `build_report_context()` reflects the board
order produced entirely through the HTTP API (document order == board order, PLAN.md §4/§9).

Also covers the Lotek-adoption UX surface (docs/LOTEK_ADOPTION.md §4): `delete_finding` (and that it
takes its artifacts -- DB rows AND on-disk files -- with it, unlike `delete_group`'s detach); threading
the optional host `extras['current_actor']` hook into `created_by` on create; and the
`scribble_can_write` context-processor value (+ its effect on the rendered board/create-engagement HTML)
driven by the optional host `extras['can_write']` hook.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from sqlalchemy import select

from scribble.artifacts_storage import resolve_path
from scribble.blueprint import _inject_base
from scribble.content import schema
from scribble.enums import Confidence, FindingStatus, OrderMode, Severity
from scribble.models import (
    Artifact,
    AssessmentType,
    Client,
    Engagement,
    EngagementFinding,
    FindingGroup,
    VulnerabilityTemplate,
)
from scribble.reporting import build_report_context

API = "/scribble/api"
UI = "/scribble"


def _make_template(db, name: str, severity: Severity = Severity.medium) -> VulnerabilityTemplate:
    tmpl = VulnerabilityTemplate(
        name=name,
        category="Test",
        default_severity=severity,
        content_json={"description": schema.doc_from_text(f"{name} description.")},
    )
    db.add(tmpl)
    db.commit()
    return tmpl


def _make_engagement(db, name: str = "Q3 Assessment") -> Engagement:
    client = Client(name=f"{name} Client")
    db.add(client)
    db.flush()
    eng = Engagement(name=name, client_id=client.id, company_name=f"{name} Corp")
    db.add(eng)
    db.commit()
    return eng


def _make_group(
    db, engagement, name, order_index: int = 0, order_mode: OrderMode = OrderMode.auto_severity
) -> FindingGroup:
    group = FindingGroup(engagement=engagement, name=name, order_index=order_index, order_mode=order_mode)
    db.add(group)
    db.commit()
    return group


# ------------------------------------------------------------------------------- engagement CRUD


def test_create_engagement_with_new_client(client, session_factory):
    resp = client.post(
        f"{UI}/engagements/new",
        data={
            "name": "New Co Pentest",
            "new_client_name": "New Co",
            "scope_type": "external",
            "company_name": "New Co Inc",
        },
    )
    assert resp.status_code == 302

    with session_factory() as db:
        eng = db.query(Engagement).filter_by(name="New Co Pentest").one()
        resolved_client = eng.resolve_client(db)
        assert resolved_client is not None
        assert resolved_client.name == "New Co"
        assert eng.company_name == "New Co Inc"
        assert eng.scope_type == "external"


def test_create_engagement_with_existing_client_allows_concurrent_engagements(client, session_factory):
    with session_factory() as db:
        existing = Client(name="Existing Co")
        db.add(existing)
        db.commit()
        existing_id = existing.id

    client.post(f"{UI}/engagements/new", data={"name": "First Engagement", "client_id": str(existing_id)})
    resp = client.post(
        f"{UI}/engagements/new", data={"name": "Second Engagement", "client_id": str(existing_id)}
    )
    assert resp.status_code == 302

    with session_factory() as db:
        count = db.query(Engagement).filter_by(client_id=existing_id).count()
    assert count == 2


def test_create_engagement_requires_name(client, session_factory):
    resp = client.post(f"{UI}/engagements/new", data={"name": ""})
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.query(Engagement).count() == 0


def test_engagement_board_404_for_missing_engagement(client):
    resp = client.get(f"{UI}/engagements/999999")
    assert resp.status_code == 404


def test_engagement_board_renders_empty_engagement(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        eng_id = eng.id
    resp = client.get(f"{UI}/engagements/{eng_id}")
    assert resp.status_code == 200
    assert b"No groups yet" in resp.data


# ------------------------------------------------------------------------------- groups


def test_create_group(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        eng_id = eng.id

    resp = client.post(f"{UI}/engagements/{eng_id}/groups", data={"name": "Internal"})
    assert resp.status_code == 302

    with session_factory() as db:
        groups = db.query(FindingGroup).filter_by(engagement_id=eng_id).all()
        assert len(groups) == 1
        assert groups[0].name == "Internal"
        assert groups[0].order_mode == OrderMode.auto_severity
        assert groups[0].include_in_report is True


def test_create_group_with_assessment_type(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        at = AssessmentType(name="Web App Test", slug="webapp-board-test", color="#000", default_order=0)
        db.add(at)
        db.commit()
        eng_id, at_id = eng.id, at.id

    client.post(
        f"{UI}/engagements/{eng_id}/groups",
        data={"name": "Web App Findings", "assessment_type_id": str(at_id)},
    )
    with session_factory() as db:
        group = db.query(FindingGroup).filter_by(engagement_id=eng_id).one()
        assert group.assessment_type_id == at_id


def test_create_group_requires_name(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        eng_id = eng.id

    resp = client.post(f"{UI}/engagements/{eng_id}/groups", data={"name": "   "})
    assert resp.status_code == 302
    with session_factory() as db:
        assert db.query(FindingGroup).filter_by(engagement_id=eng_id).count() == 0


def test_delete_group_detaches_findings_instead_of_deleting_them(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        tmpl = _make_template(db, "Plaintext Creds")
        finding = EngagementFinding.from_template(tmpl, engagement_id=eng.id, group_id=group.id)
        db.add(finding)
        db.commit()
        eng_id, group_id, finding_id = eng.id, group.id, finding.id

    resp = client.post(f"{UI}/engagements/{eng_id}/groups/{group_id}/delete")
    assert resp.status_code == 302

    with session_factory() as db:
        assert db.get(FindingGroup, group_id) is None
        finding = db.get(EngagementFinding, finding_id)
        assert finding is not None
        assert finding.group_id is None


def test_delete_group_wrong_engagement_404(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        other = _make_engagement(db, name="Other")
        group = _make_group(db, other, "Foreign")
        eng_id, group_id = eng.id, group.id

    resp = client.post(f"{UI}/engagements/{eng_id}/groups/{group_id}/delete")
    assert resp.status_code == 404


# ------------------------------------------------------------------------------- add finding from template


def test_add_finding_from_template_into_group(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "External")
        tmpl = _make_template(db, "SQL Injection", Severity.critical)
        eng_id, group_id, tmpl_id = eng.id, group.id, tmpl.id

    resp = client.post(
        f"{UI}/engagements/{eng_id}/findings",
        data={"template_id": str(tmpl_id), "group_id": str(group_id)},
    )
    assert resp.status_code == 302

    with session_factory() as db:
        finding = db.query(EngagementFinding).filter_by(engagement_id=eng_id).one()
        assert finding.group_id == group_id
        assert finding.template_id == tmpl_id
        assert finding.title == "SQL Injection"
        assert finding.severity == Severity.critical
        tmpl = db.get(VulnerabilityTemplate, tmpl_id)
        assert schema.plain_text(finding.content_json["description"]) == schema.plain_text(
            tmpl.content_json["description"]
        )


def test_add_findings_into_two_groups(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        internal = _make_group(db, eng, "Internal", order_index=0)
        external = _make_group(db, eng, "External", order_index=1)
        t1 = _make_template(db, "Weak Kerberos", Severity.high)
        t2 = _make_template(db, "Reflected XSS", Severity.medium)
        eng_id = eng.id
        internal_id, external_id = internal.id, external.id
        t1_id, t2_id = t1.id, t2.id

    client.post(
        f"{UI}/engagements/{eng_id}/findings", data={"template_id": str(t1_id), "group_id": str(internal_id)}
    )
    client.post(
        f"{UI}/engagements/{eng_id}/findings", data={"template_id": str(t2_id), "group_id": str(external_id)}
    )

    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        by_group = {g.name: [f.title for f in g.findings] for g in eng.groups}
    assert by_group["Internal"] == ["Weak Kerberos"]
    assert by_group["External"] == ["Reflected XSS"]


def test_add_finding_without_group_is_ungrouped(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        tmpl = _make_template(db, "Open Redirect")
        eng_id, tmpl_id = eng.id, tmpl.id

    client.post(f"{UI}/engagements/{eng_id}/findings", data={"template_id": str(tmpl_id)})

    with session_factory() as db:
        finding = db.query(EngagementFinding).filter_by(engagement_id=eng_id).one()
        assert finding.group_id is None


def test_add_finding_rejects_group_from_another_engagement(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        other = _make_engagement(db, name="Other")
        foreign_group = _make_group(db, other, "Foreign")
        tmpl = _make_template(db, "CSRF")
        eng_id, foreign_group_id, tmpl_id = eng.id, foreign_group.id, tmpl.id

    client.post(
        f"{UI}/engagements/{eng_id}/findings",
        data={"template_id": str(tmpl_id), "group_id": str(foreign_group_id)},
    )

    with session_factory() as db:
        finding = db.query(EngagementFinding).filter_by(engagement_id=eng_id).one()
        # Never attach to a group belonging to a different engagement -- falls back to ungrouped.
        assert finding.group_id is None


# ------------------------------------------------------------------------------- finding detail page


def test_finding_detail_get_renders_editor_and_gallery(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        t = _make_template(db, "XSS", Severity.high)
        f = EngagementFinding.from_template(t, engagement_id=eng.id)
        db.add(f)
        db.commit()
        f_id = f.id

    resp = client.get(f"{UI}/findings/{f_id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert f'data-finding-id="{f_id}"' in body
    assert "scribble-gallery" in body
    assert 'data-block="description"' in body
    assert 'data-block="remediation"' in body


def test_finding_detail_404_for_missing(client):
    resp = client.get(f"{UI}/findings/999999")
    assert resp.status_code == 404


def test_finding_detail_update_meta(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        t = _make_template(db, "XSS", Severity.medium)
        f = EngagementFinding.from_template(t, engagement_id=eng.id)
        db.add(f)
        db.commit()
        f_id = f.id

    resp = client.post(
        f"{UI}/findings/{f_id}",
        data={
            "title": "Stored XSS in comments",
            "severity": "critical",
            "confidence": "high",
            "status": "triaged",
            "cvss_score": "9.1",
            "cvss_vector": "AV:N/AC:L",
            "target_host": "app.example.test",
            "target_port": "443",
            "target_url": "https://app.example.test/comments",
            # include_in_report intentionally omitted -> should become unchecked/False
        },
    )
    assert resp.status_code == 302

    with session_factory() as db:
        finding = db.get(EngagementFinding, f_id)
        assert finding.title == "Stored XSS in comments"
        assert finding.severity == Severity.critical
        assert finding.confidence == Confidence.high
        assert finding.status == FindingStatus.triaged
        assert finding.cvss_score == 9.1
        assert finding.target_host == "app.example.test"
        assert finding.include_in_report is False


# ------------------------------------------------------------------------------- group reorder (persists)


def test_reorder_groups_persists_new_order(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        g1 = _make_group(db, eng, "A", order_index=0)
        g2 = _make_group(db, eng, "B", order_index=1)
        g3 = _make_group(db, eng, "C", order_index=2)
        eng_id = eng.id
        ids = [g1.id, g2.id, g3.id]

    new_order = [ids[2], ids[0], ids[1]]
    resp = client.post(f"{API}/engagements/{eng_id}/groups/reorder", json={"order": new_order})
    assert resp.status_code == 200

    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        assert [g.id for g in eng.groups] == new_order
        assert [g.order_index for g in eng.groups] == [0, 1, 2]


def test_reorder_groups_ignores_stale_and_foreign_ids_and_appends_missing(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        other_eng = _make_engagement(db, name="Other")
        g1 = _make_group(db, eng, "A", order_index=0)
        g2 = _make_group(db, eng, "B", order_index=1)
        foreign_group = _make_group(db, other_eng, "Foreign", order_index=0)
        eng_id = eng.id
        g1_id, g2_id, foreign_id = g1.id, g2.id, foreign_group.id

    # Payload: put g2 first, include a nonexistent id and a group from a different engagement -- none
    # of that should crash the endpoint or corrupt either engagement's state.
    resp = client.post(
        f"{API}/engagements/{eng_id}/groups/reorder",
        json={"order": [g2_id, 999999, foreign_id]},
    )
    assert resp.status_code == 200

    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        ordered_ids = [g.id for g in eng.groups]
        # g2 honored first (explicitly requested); g1 (unmentioned) appended after. The
        # nonexistent/foreign ids never leak into this engagement's ordering.
        assert ordered_ids == [g2_id, g1_id]
        assert [g.order_index for g in eng.groups] == [0, 1]

        # The other engagement's group is untouched.
        other = db.get(FindingGroup, foreign_id)
        assert other.order_index == 0


def test_reorder_groups_on_empty_engagement_is_a_noop(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        eng_id = eng.id

    resp = client.post(f"{API}/engagements/{eng_id}/groups/reorder", json={"order": []})
    assert resp.status_code == 200
    assert resp.get_json()["order"] == []


def test_reorder_groups_missing_engagement_404(client):
    resp = client.post(f"{API}/engagements/999999/groups/reorder", json={"order": []})
    assert resp.status_code == 404


def test_reorder_groups_requires_order_list(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        eng_id = eng.id
    resp = client.post(f"{API}/engagements/{eng_id}/groups/reorder", json={"order": "nope"})
    assert resp.status_code == 400


# ------------------------------------------------------------------------------- finding move (cross-group)


def test_move_finding_across_groups_updates_group_and_reindexes_both_sides(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        internal = _make_group(db, eng, "Internal", order_index=0)
        external = _make_group(db, eng, "External", order_index=1)
        t = _make_template(db, "Weak SMB Signing", Severity.low)
        f1 = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=internal.id, order_index=0)
        f2 = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=internal.id, order_index=1)
        db.add(f1)
        db.add(f2)
        db.commit()
        internal_id, external_id = internal.id, external.id
        f1_id, f2_id = f1.id, f2.id

    resp = client.post(f"{API}/findings/{f1_id}/move", json={"group_id": external_id, "order_index": 0})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["finding"]["group_id"] == external_id
    assert body["finding"]["order_index"] == 0
    assert body["group"]["id"] == external_id
    assert body["previous_group"]["id"] == internal_id

    with session_factory() as db:
        moved = db.get(EngagementFinding, f1_id)
        assert moved.group_id == external_id
        assert moved.order_index == 0

        # The source group's remaining finding is reindexed with no gap left behind.
        remaining = db.get(EngagementFinding, f2_id)
        assert remaining.group_id == internal_id
        assert remaining.order_index == 0


def test_move_finding_into_nonexistent_group_404(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        t = _make_template(db, "X")
        f = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=group.id)
        db.add(f)
        db.commit()
        f_id = f.id

    resp = client.post(f"{API}/findings/{f_id}/move", json={"group_id": 999999, "order_index": 0})
    assert resp.status_code == 404
    with session_factory() as db:
        # The finding must not have moved when the target group doesn't exist.
        assert db.get(EngagementFinding, f_id).group_id is not None


def test_move_finding_into_group_from_another_engagement_rejected(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        other_eng = _make_engagement(db, name="Other")
        other_group = _make_group(db, other_eng, "Other Group")
        t = _make_template(db, "X")
        f = EngagementFinding.from_template(t, engagement_id=eng.id)
        db.add(f)
        db.commit()
        f_id, other_group_id = f.id, other_group.id

    resp = client.post(f"{API}/findings/{f_id}/move", json={"group_id": other_group_id, "order_index": 0})
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.get(EngagementFinding, f_id).group_id is None


def test_move_nonexistent_finding_404(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        group_id = group.id
    resp = client.post(f"{API}/findings/999999/move", json={"group_id": group_id, "order_index": 0})
    assert resp.status_code == 404


def test_move_finding_requires_group_id_key(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        t = _make_template(db, "X")
        f = EngagementFinding.from_template(t, engagement_id=eng.id)
        db.add(f)
        db.commit()
        f_id = f.id
    resp = client.post(f"{API}/findings/{f_id}/move", json={"order_index": 0})
    assert resp.status_code == 400


def test_move_finding_to_ungrouped(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        t = _make_template(db, "X")
        f = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=group.id)
        db.add(f)
        db.commit()
        f_id = f.id

    resp = client.post(f"{API}/findings/{f_id}/move", json={"group_id": None, "order_index": 0})
    assert resp.status_code == 200
    assert resp.get_json()["group"] is None

    with session_factory() as db:
        finding = db.get(EngagementFinding, f_id)
        assert finding.group_id is None


def test_move_finding_within_same_group_reorders(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        t = _make_template(db, "X")
        f1 = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=group.id, order_index=0)
        f2 = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=group.id, order_index=1)
        f3 = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=group.id, order_index=2)
        db.add_all([f1, f2, f3])
        db.commit()
        group_id = group.id
        f1_id, f2_id, f3_id = f1.id, f2.id, f3.id

    # Move the last finding to the front.
    resp = client.post(f"{API}/findings/{f3_id}/move", json={"group_id": group_id, "order_index": 0})
    assert resp.status_code == 200
    assert resp.get_json()["previous_group"] is None  # same group -> no "source" side to report

    with session_factory() as db:
        group = db.get(FindingGroup, group_id)
        ordered = sorted(group.findings, key=lambda f: f.order_index)
        assert [f.id for f in ordered] == [f3_id, f1_id, f2_id]
        assert [f.order_index for f in ordered] == [0, 1, 2]


# ------------------------------------------------------------------------------- auto <-> manual order_mode


def test_first_manual_drag_flips_group_to_manual(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal", order_mode=OrderMode.auto_severity)
        t = _make_template(db, "X")
        f1 = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=group.id, order_index=0)
        f2 = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=group.id, order_index=1)
        db.add_all([f1, f2])
        db.commit()
        group_id, f2_id = group.id, f2.id

    with session_factory() as db:
        assert db.get(FindingGroup, group_id).order_mode == OrderMode.auto_severity

    resp = client.post(f"{API}/findings/{f2_id}/move", json={"group_id": group_id, "order_index": 0})
    assert resp.status_code == 200
    assert resp.get_json()["group"]["order_mode"] == "manual"

    with session_factory() as db:
        assert db.get(FindingGroup, group_id).order_mode == OrderMode.manual


def test_manual_drag_persists_the_visual_order_not_order_index_order(client, session_factory):
    """C1 regression (adversarial review): the client's drop `order_index` is a slot in the RENDERED
    order, which for an auto_severity group is SEVERITY order, not stored-order_index order. Moving a
    finding must insert it among the neighbors the user actually saw, freeze THAT order, and have both
    the persisted order_index values and build_report_context agree. Uses THREE DISTINCT severities with
    a stored order_index that deliberately disagrees with severity order, so an order_index-based insert
    (the pre-fix bug) produces a visibly different result than a display-order insert.
    """
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal", order_mode=OrderMode.auto_severity)
        # Stored order_index (0,1,2) is the *reverse-ish* of severity worst-first, so the two orders
        # genuinely disagree: by order_index -> [medium, critical, low]; by severity -> [critical,
        # medium, low].
        t_med = _make_template(db, "Medium Finding", Severity.medium)
        t_crit = _make_template(db, "Critical Finding", Severity.critical)
        t_low = _make_template(db, "Low Finding", Severity.low)
        med = EngagementFinding.from_template(t_med, engagement_id=eng.id, group_id=group.id, order_index=0)
        crit = EngagementFinding.from_template(t_crit, engagement_id=eng.id, group_id=group.id, order_index=1)
        low = EngagementFinding.from_template(t_low, engagement_id=eng.id, group_id=group.id, order_index=2)
        db.add_all([med, crit, low])
        db.commit()
        eng_id, group_id = eng.id, group.id
        med_id, crit_id, low_id = med.id, crit.id, low.id

    # What the board actually renders (auto_severity, worst-first): [Critical, Medium, Low].
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eng_id))
    displayed = [f.title for f in next(g for g in ctx.groups if g.name == "Internal").findings]
    assert displayed == ["Critical Finding", "Medium Finding", "Low Finding"]

    # The user drags Low into the MIDDLE, intending [Critical, Low, Medium]. In the rendered DOM the
    # remaining neighbors are [Critical, Medium]; dropping between them is client index 1 (board.js reads
    # the DOM position). Pre-fix, the server sorted the neighbors by order_index -> [Medium, Critical]
    # and inserted Low at 1 -> [Medium, Low, Critical], the WRONG order the user never chose.
    resp = client.post(f"{API}/findings/{low_id}/move", json={"group_id": group_id, "order_index": 1})
    assert resp.status_code == 200
    assert resp.get_json()["group"]["order_mode"] == "manual"

    # Persisted order_index must encode the VISUAL order the user built: Critical, Low, Medium.
    with session_factory() as db:
        group = db.get(FindingGroup, group_id)
        ordered = sorted(group.findings, key=lambda f: f.order_index)
        assert [f.id for f in ordered] == [crit_id, low_id, med_id]
        ctx = build_report_context(db.get(Engagement, eng_id))
    result = [f.title for f in next(g for g in ctx.groups if g.name == "Internal").findings]
    assert result == ["Critical Finding", "Low Finding", "Medium Finding"]


def test_move_into_a_different_group_flips_destination_not_source(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        internal = _make_group(db, eng, "Internal", order_mode=OrderMode.auto_severity)
        external = _make_group(db, eng, "External", order_mode=OrderMode.auto_severity)
        t = _make_template(db, "X")
        f = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=internal.id)
        db.add(f)
        db.commit()
        internal_id, external_id, f_id = internal.id, external.id, f.id

    resp = client.post(f"{API}/findings/{f_id}/move", json={"group_id": external_id, "order_index": 0})
    assert resp.status_code == 200

    with session_factory() as db:
        assert db.get(FindingGroup, external_id).order_mode == OrderMode.manual
        # A group nothing was dragged into during this call keeps its own order_mode untouched.
        assert db.get(FindingGroup, internal_id).order_mode == OrderMode.auto_severity


def test_rerank_resets_group_to_auto_severity(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal", order_mode=OrderMode.manual)
        group_id = group.id

    resp = client.post(f"{API}/groups/{group_id}", json={"order_mode": "auto_severity"})
    assert resp.status_code == 200
    assert resp.get_json()["order_mode"] == "auto_severity"

    with session_factory() as db:
        assert db.get(FindingGroup, group_id).order_mode == OrderMode.auto_severity


# ------------------------------------------------------------------------------- group update (rename/incl.)


def test_update_group_rename_and_include_toggle(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        group_id = group.id

    resp = client.post(
        f"{API}/groups/{group_id}", json={"name": "Internal Network", "include_in_report": False}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "Internal Network"
    assert body["include_in_report"] is False

    with session_factory() as db:
        group = db.get(FindingGroup, group_id)
        assert group.name == "Internal Network"
        assert group.include_in_report is False


def test_update_group_rejects_invalid_order_mode(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        group_id = group.id
    resp = client.post(f"{API}/groups/{group_id}", json={"order_mode": "not-a-real-mode"})
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.get(FindingGroup, group_id).order_mode == OrderMode.auto_severity


def test_update_group_missing_404(client):
    resp = client.post(f"{API}/groups/999999", json={"name": "x"})
    assert resp.status_code == 404


def test_update_group_rejects_empty_name(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        group_id = group.id
    resp = client.post(f"{API}/groups/{group_id}", json={"name": "   "})
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.get(FindingGroup, group_id).name == "Internal"


# ------------------------------------------------------------------------------- board order == doc order


def test_board_order_matches_report_context_order_end_to_end(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        internal = _make_group(db, eng, "Internal", order_index=0)
        external = _make_group(db, eng, "External", order_index=1)
        t_low = _make_template(db, "Low Sev Finding", Severity.low)
        t_crit = _make_template(db, "Critical Finding", Severity.critical)
        low = EngagementFinding.from_template(
            t_low, engagement_id=eng.id, group_id=internal.id, order_index=0
        )
        crit = EngagementFinding.from_template(
            t_crit, engagement_id=eng.id, group_id=internal.id, order_index=1
        )
        db.add_all([low, crit])
        db.commit()
        eng_id = eng.id
        internal_id, external_id = internal.id, external.id
        low_id, crit_id = low.id, crit.id

    # Sanity: before any manual intervention, auto_severity would put Critical before Low.
    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
    internal_ctx = next(g for g in ctx.groups if g.name == "Internal")
    assert [f.title for f in internal_ctx.findings] == ["Critical Finding", "Low Sev Finding"]

    # 1. Reorder groups via the API: External should now render before Internal.
    resp = client.post(
        f"{API}/engagements/{eng_id}/groups/reorder", json={"order": [external_id, internal_id]}
    )
    assert resp.status_code == 200

    # 2. Drag the low-severity finding above the critical one -- a manual override against severity
    #    order -- via the move API. This must flip Internal to manual and stick in the report.
    resp = client.post(f"{API}/findings/{low_id}/move", json={"group_id": internal_id, "order_index": 0})
    assert resp.status_code == 200
    assert resp.get_json()["group"]["order_mode"] == "manual"

    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)

    # Group order reflects the reorder call (External first) -- board order == document order.
    assert [g.name for g in ctx.groups] == ["External", "Internal"]

    # Within the now-manual Internal group, the low-severity finding stays first: manual order_index
    # wins over the auto_severity worst-first rule it would otherwise have used.
    internal_ctx = next(g for g in ctx.groups if g.name == "Internal")
    assert [f.title for f in internal_ctx.findings] == ["Low Sev Finding", "Critical Finding"]

    # Also verify the board's own rendered HTML shows the same order an author would see. Match on the
    # `data-*-id` markers (not the group/finding names) because the page's other forms -- the "add a
    # group" assessment-type picker (seeded with defaults named "Internal"/"External") and the "add a
    # finding" template picker (listing template names alphabetically) -- legitimately mention the same
    # words earlier in the document without saying anything about board order.
    board_resp = client.get(f"{UI}/engagements/{eng_id}")
    body = board_resp.data.decode()
    assert body.index(f'data-group-id="{external_id}"') < body.index(f'data-group-id="{internal_id}"')
    assert body.index(f'data-finding-id="{low_id}"') < body.index(f'data-finding-id="{crit_id}"')


# ------------------------------------------------------------------------------- delete finding


def test_delete_finding_removes_finding_and_its_artifacts(client, session_factory, app):
    cfg = app.extensions["scribble"]
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        tmpl = _make_template(db, "Stored XSS")
        finding = EngagementFinding.from_template(tmpl, engagement_id=eng.id, group_id=group.id)
        db.add(finding)
        db.commit()
        eng_id, finding_id = eng.id, finding.id

    # Give the finding a real on-disk artifact via the actual upload API (not a hand-built row) so the
    # test proves the real end-state: both the DB row AND the file it points at are gone afterward, not
    # just that a query returns fewer rows.
    upload = client.post(
        f"{API}/artifacts",
        data={
            "engagement_id": str(eng_id),
            "finding_id": str(finding_id),
            "file": (io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16), "evidence.png"),
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code == 201
    artifact_id = upload.get_json()["id"]

    with session_factory() as db:
        artifact = db.get(Artifact, artifact_id)
        on_disk = resolve_path(cfg, artifact.storage_path)
    assert on_disk.is_file()

    resp = client.post(f"{UI}/engagements/{eng_id}/findings/{finding_id}/delete")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/engagements/{eng_id}")

    with session_factory() as db:
        assert db.get(EngagementFinding, finding_id) is None
        assert db.get(Artifact, artifact_id) is None
    assert not on_disk.is_file()


def test_delete_finding_detaches_its_nested_children(client, session_factory):
    """The cookie board's delete hit the same self-FK wall as the machine route (both call
    `findings_service.delete_finding`): deleting a promoted PARENT raised `IntegrityError` and the request
    500'd, because `EngagementFinding.parent_id` has no `ondelete` and no ORM relationship to clear it.

    Children are detached, not deleted — see `findings_service.detach_children`. Driven through the HTTP
    route rather than the service so it proves the surface a user actually clicks.
    """
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Active Directory")
        tmpl = _make_template(db, "Kerberoasting")
        parent = EngagementFinding.from_template(tmpl, engagement_id=eng.id, group_id=group.id)
        db.add(parent)
        db.flush()
        children = [
            EngagementFinding.from_template(
                tmpl, engagement_id=eng.id, group_id=group.id, parent_id=parent.id,
                target_host=host, order_index=index + 1,
            )
            for index, host in enumerate(("10.0.0.10", "10.0.0.20"))
        ]
        db.add_all(children)
        db.commit()
        eng_id, parent_id = eng.id, parent.id
        child_ids = [child.id for child in children]

    resp = client.post(f"{UI}/engagements/{eng_id}/findings/{parent_id}/delete")
    assert resp.status_code == 302

    with session_factory() as db:
        assert db.get(EngagementFinding, parent_id) is None
        for child_id in child_ids:
            child = db.get(EngagementFinding, child_id)
            assert child is not None and child.parent_id is None


def test_engagement_delete_survives_a_promoted_parent_child_cluster(client, session_factory):
    """Deleting an ENGAGEMENT that holds a promoted aggregation must work.

    Pre-existing defect of the same class as the parent-delete one, found while fixing it and not reported:
    `Engagement.findings` cascades `delete-orphan`, and with no ORM relationship on the self-FK SQLAlchemy
    has no dependency to order those DELETEs by — it emits them in one batch and the child rows' `parent_id`
    FK fails. So an engagement holding ANY promoted finding could not be deleted at all (`IntegrityError`
    here, `ForeignKeyViolation` on prod Postgres). `findings_service.flatten_nesting` clears the links first.
    """
    with session_factory() as db:
        eng = _make_engagement(db)
        tmpl = _make_template(db, "SMB signing not required")
        parent = EngagementFinding.from_template(tmpl, engagement_id=eng.id)
        db.add(parent)
        db.flush()
        db.add_all([
            EngagementFinding.from_template(
                tmpl, engagement_id=eng.id, parent_id=parent.id, target_host=f"10.0.0.{n}"
            )
            for n in (5, 6, 7)
        ])
        db.commit()
        eng_id = eng.id

    resp = client.post(f"{UI}/engagements/{eng_id}/delete")
    assert resp.status_code == 302

    with session_factory() as db:
        assert db.get(Engagement, eng_id) is None
        assert db.scalars(
            select(EngagementFinding).where(EngagementFinding.engagement_id == eng_id)
        ).all() == []


def test_engagement_delete_survives_rows_that_reference_a_finding_from_OUTSIDE_the_cascade(
    client, session_factory
):
    """Deleting an engagement must work when something outside its ORM cascade graph references a finding.

    `Engagement` cascades `delete-orphan` to groups/findings/artifacts/variable_values/checklists — the rows
    the ORM knows are the engagement's. It knows nothing about a `CollabDoc` (which has no engagement column
    at all, and which the live co-editing room writes the moment a human opens a block), a finding-scoped
    `VariableValue` whose `engagement_id` is NULL, or the ORDER of an `EngagementChecklistItem`'s DELETE
    relative to the findings' (no dependency edge between those mappers). Each of those made this route a
    **500** — the engagement could not be deleted at all — which is the same defect
    `test_engagement_delete_survives_a_promoted_parent_child_cluster` above covers for `parent_id` only. That
    test passing is exactly why this one was missing: one member of the FK set was fixed and certified.

    `findings_service.prepare_engagement_delete` owns the whole set now; see the enumeration at the top of
    that module.
    """
    from scribble.models import (
        CollabDoc,
        EngagementChecklist,
        EngagementChecklistItem,
        TemplateVariable,
        VariableValue,
    )

    with session_factory() as db:
        eng = _make_engagement(db)
        tmpl = _make_template(db, "SMB signing not required")
        finding = EngagementFinding.from_template(tmpl, engagement_id=eng.id)
        db.add(finding)
        db.flush()
        variable = TemplateVariable(key="PER_FINDING_HOST", label="Host")  # not a seeded key
        checklist = EngagementChecklist(engagement_id=eng.id, name="Coverage")
        db.add_all([variable, checklist, CollabDoc(finding_id=finding.id, block="description",
                                                  ydoc_state=b"\x00\x01ydoc")])
        db.flush()
        db.add_all([
            VariableValue(variable_id=variable.id, finding_id=finding.id, value="10.0.0.5"),
            EngagementChecklistItem(
                engagement_checklist_id=checklist.id, text="SMB reviewed", finding_id=finding.id
            ),
        ])
        db.commit()
        eng_id, finding_id = eng.id, finding.id

    resp = client.post(f"{UI}/engagements/{eng_id}/delete")
    assert resp.status_code == 302

    with session_factory() as db:
        assert db.get(Engagement, eng_id) is None
        assert db.get(EngagementFinding, finding_id) is None
        assert db.scalars(select(CollabDoc)).all() == []
        assert db.scalars(select(VariableValue)).all() == []
        # The checklist went with the engagement (it IS the engagement's), items and all.
        assert db.scalars(select(EngagementChecklistItem)).all() == []


def test_engagement_delete_clears_the_one_referrer_no_relationship_cascades(client, session_factory):
    """`scribble_report_renders.engagement_id` — the sixth column referencing an engagement, and the one no
    ORM relationship covers — must not block the delete either.

    NOT reported by either review round: found by asking the enumeration question ONE TABLE OVER, which is the
    lesson round 2 was about. Five of the six referrers are reached by an `Engagement` relationship cascading
    `delete-orphan`; `ReportRender` has no relationship at all, so a single row made this route a 500
    (reproduced) — an FK violation that deletes nothing.

    LATENT today: nothing in the codebase instantiates `ReportRender` (the table is schema-frozen for a later
    phase), which is exactly why it would have shipped — the first code to write one would have broken
    engagement delete somewhere far from itself. Fixed in `findings_service.prepare_engagement_delete`, with
    the file-unlink obligation for a future writer recorded next to `_ENGAGEMENT_UNCASCADED`.
    """
    from scribble.enums import ReportFormat
    from scribble.models import ReportRender

    with session_factory() as db:
        eng = _make_engagement(db)
        tmpl = _make_template(db, "Missing HSTS")
        db.add(EngagementFinding.from_template(tmpl, engagement_id=eng.id))
        db.add(ReportRender(engagement_id=eng.id, format=ReportFormat.html, path="renders/1.html"))
        db.commit()
        eng_id = eng.id

    resp = client.post(f"{UI}/engagements/{eng_id}/delete")
    assert resp.status_code == 302

    with session_factory() as db:
        assert db.get(Engagement, eng_id) is None
        assert db.scalars(select(ReportRender)).all() == []


def test_every_column_referencing_an_engagement_is_cascaded_or_declared():
    """The engagement-side twin of the finding-side FK guard, for the same reason.

    A guard that enumerates the columns pointing at ONE table and stops is the same shape as a fix applied to
    one member of a set: it certifies the area it was written for and says nothing about its neighbour. So
    this derives the referrers of `scribble_engagements.id` from `Base.metadata` and requires each to be
    reachable by an `Engagement` relationship that cascades `delete-orphan` OR declared in
    `findings_service._ENGAGEMENT_UNCASCADED` (which `prepare_engagement_delete` clears by hand). A new table
    referencing an engagement fails this until someone decides which it is.
    """
    from scribble import findings_service as svc
    from scribble.models import Base, Engagement

    referrers = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        for fk in column.foreign_keys
        if fk.column.table.name == "scribble_engagements" and fk.column.name == "id"
    }
    cascaded = {
        (rel.mapper.local_table.name, "engagement_id")
        for rel in Engagement.__mapper__.relationships
        if rel.cascade.delete_orphan
    }
    declared = cascaded | {(m.__tablename__, "engagement_id") for m in svc._ENGAGEMENT_UNCASCADED}
    assert referrers == declared, (
        "a column referencing scribble_engagements.id is neither cascade-covered nor cleared by hand: "
        f"{referrers ^ declared} — give it a relationship with cascade='all, delete-orphan' or add it to "
        "findings_service._ENGAGEMENT_UNCASCADED, or deleting an engagement will 500"
    )


def test_delete_finding_wrong_engagement_404(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        other = _make_engagement(db, name="Other")
        tmpl = _make_template(db, "CSRF")
        finding = EngagementFinding.from_template(tmpl, engagement_id=other.id)
        db.add(finding)
        db.commit()
        eng_id, finding_id = eng.id, finding.id

    resp = client.post(f"{UI}/engagements/{eng_id}/findings/{finding_id}/delete")
    assert resp.status_code == 404

    with session_factory() as db:
        # Never touched -- a 404 on the wrong-engagement guard must not delete anything.
        assert db.get(EngagementFinding, finding_id) is not None


def test_delete_finding_missing_404(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        eng_id = eng.id

    resp = client.post(f"{UI}/engagements/{eng_id}/findings/999999/delete")
    assert resp.status_code == 404


def test_delete_finding_ungrouped_has_no_artifacts_is_a_noop_delete(client, session_factory):
    # A finding with no artifacts at all must delete cleanly (the artifact-cleanup loop over an empty
    # list is a no-op, not an error).
    with session_factory() as db:
        eng = _make_engagement(db)
        tmpl = _make_template(db, "Open Redirect")
        finding = EngagementFinding.from_template(tmpl, engagement_id=eng.id)
        db.add(finding)
        db.commit()
        eng_id, finding_id = eng.id, finding.id

    resp = client.post(f"{UI}/engagements/{eng_id}/findings/{finding_id}/delete")
    assert resp.status_code == 302

    with session_factory() as db:
        assert db.get(EngagementFinding, finding_id) is None


# ------------------------------------------------------------------------------- created_by threading


def test_engagement_created_by_none_without_host_hook(client, session_factory):
    resp = client.post(
        f"{UI}/engagements/new",
        data={"name": "No Host Co Pentest", "new_client_name": "No Host Co"},
    )
    assert resp.status_code == 302
    with session_factory() as db:
        eng = db.query(Engagement).filter_by(name="No Host Co Pentest").one()
        assert eng.created_by is None


def test_engagement_created_by_set_from_host_current_actor_hook(client, session_factory, app):
    cfg = app.extensions["scribble"]
    cfg.extras["current_actor"] = lambda: SimpleNamespace(username="j.analyst")
    try:
        resp = client.post(
            f"{UI}/engagements/new",
            data={"name": "Hosted Co Pentest", "new_client_name": "Hosted Co"},
        )
        assert resp.status_code == 302
        with session_factory() as db:
            eng = db.query(Engagement).filter_by(name="Hosted Co Pentest").one()
            assert eng.created_by == "j.analyst"
    finally:
        cfg.extras.pop("current_actor", None)


def test_finding_created_by_none_without_host_hook(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        tmpl = _make_template(db, "Unauthenticated API")
        eng_id, tmpl_id = eng.id, tmpl.id

    client.post(f"{UI}/engagements/{eng_id}/findings", data={"template_id": str(tmpl_id)})

    with session_factory() as db:
        finding = db.query(EngagementFinding).filter_by(engagement_id=eng_id).one()
        assert finding.created_by is None


def test_finding_created_by_set_from_host_current_actor_hook(client, session_factory, app):
    cfg = app.extensions["scribble"]
    cfg.extras["current_actor"] = lambda: SimpleNamespace(username="j.analyst")
    try:
        with session_factory() as db:
            eng = _make_engagement(db)
            tmpl = _make_template(db, "Unauthenticated API")
            eng_id, tmpl_id = eng.id, tmpl.id

        client.post(f"{UI}/engagements/{eng_id}/findings", data={"template_id": str(tmpl_id)})

        with session_factory() as db:
            finding = db.query(EngagementFinding).filter_by(engagement_id=eng_id).one()
            assert finding.created_by == "j.analyst"
    finally:
        cfg.extras.pop("current_actor", None)


def test_created_by_none_when_current_actor_hook_raises(client, session_factory, app):
    cfg = app.extensions["scribble"]

    def boom():
        raise RuntimeError("host session backend down")

    cfg.extras["current_actor"] = boom
    try:
        resp = client.post(
            f"{UI}/engagements/new",
            data={"name": "Flaky Host Co Pentest", "new_client_name": "Flaky Host Co"},
        )
        assert resp.status_code == 302
        with session_factory() as db:
            eng = db.query(Engagement).filter_by(name="Flaky Host Co Pentest").one()
            # A misbehaving host hook is an attribution nicety failure, never a write failure.
            assert eng.created_by is None
    finally:
        cfg.extras.pop("current_actor", None)


# ------------------------------------------------------------------------------- scribble_can_write gating


def test_scribble_can_write_defaults_true_without_host_hook(app):
    with app.app_context():
        assert _inject_base()["scribble_can_write"] is True


def test_scribble_can_write_reflects_host_can_write_hook(app):
    cfg = app.extensions["scribble"]
    cfg.extras["can_write"] = lambda: False
    try:
        with app.app_context():
            assert _inject_base()["scribble_can_write"] is False
    finally:
        cfg.extras.pop("can_write", None)


def test_board_hides_mutating_controls_for_read_only_viewer(client, session_factory, app):
    cfg = app.extensions["scribble"]
    with session_factory() as db:
        eng = _make_engagement(db)
        group = _make_group(db, eng, "Internal")
        tmpl = _make_template(db, "Weak TLS Config")
        finding = EngagementFinding.from_template(tmpl, engagement_id=eng.id, group_id=group.id)
        db.add(finding)
        db.commit()
        eng_id = eng.id

    # Markers unique to the RENDERED controls (not to the page's static <style> block, which declares
    # e.g. `.scribble-board-group-delete { margin: 0; }` unconditionally -- asserting on a CSS class
    # name alone would pass even if the gate were broken and the button always rendered).
    group_delete_confirm = "Delete this group? Its findings become ungrouped, not deleted."
    finding_delete_confirm = "Delete this finding? This also deletes its artifacts and cannot be undone."

    # Writable (default, no host hook): every mutating control renders.
    body = client.get(f"{UI}/engagements/{eng_id}").data.decode()
    assert "Add group" in body
    assert "Add finding" in body
    assert group_delete_confirm in body
    assert finding_delete_confirm in body
    assert "You have read-only access" not in body

    # Read-only (host says can_write() -> False): the mutating controls are gone, and a plain-language
    # note explains why -- never a control the click would 400/403 on.
    cfg.extras["can_write"] = lambda: False
    try:
        body = client.get(f"{UI}/engagements/{eng_id}").data.decode()
        assert "You have read-only access" in body
        assert "Add group" not in body
        assert "Add finding" not in body
        assert group_delete_confirm not in body
        assert finding_delete_confirm not in body
    finally:
        cfg.extras.pop("can_write", None)


def test_engagement_new_form_hidden_for_read_only_viewer(client, app):
    cfg = app.extensions["scribble"]
    cfg.extras["can_write"] = lambda: False
    try:
        body = client.get(f"{UI}/engagements/new").data.decode()
        assert "You have read-only access" in body
        assert "Create engagement" not in body
    finally:
        cfg.extras.pop("can_write", None)


def test_finding_meta_form_disabled_for_read_only_viewer(client, session_factory, app):
    cfg = app.extensions["scribble"]
    with session_factory() as db:
        eng = _make_engagement(db)
        tmpl = _make_template(db, "Missing Rate Limiting")
        finding = EngagementFinding.from_template(tmpl, engagement_id=eng.id)
        db.add(finding)
        db.commit()
        finding_id = finding.id

    cfg.extras["can_write"] = lambda: False
    try:
        body = client.get(f"{UI}/findings/{finding_id}").data.decode()
        assert "<fieldset" in body and "disabled" in body
        assert "You have read-only access" in body
        # The delete-finding control is a POST form -- gone entirely, not merely disabled. Match on the
        # confirm text (unique to the rendered form), not the CSS class name (declared unconditionally
        # in the page's <style> block, so it would pass even if the gate were broken).
        assert "Delete this finding? This also deletes its artifacts and cannot be undone." not in body
    finally:
        cfg.extras.pop("can_write", None)
