"""The finding editor's report-composition controls: the affected-hosts table (include / suppress /
remove / add a host) and the per-section suppress checkboxes, driven over scribble's own browser
blueprint. Backend propagation is pinned by test_report_section_suppression.py; this pins the operator
routes + that the editor surfaces them.
"""
from __future__ import annotations

import uuid

import scribble.models as fm
from scribble.content import schema
from scribble.enums import Severity

UI = "/scribble"


def _tree(session_factory):
    with session_factory() as db:
        eng = fm.ReportBoard(name="Host Editor Eng", client_id=uuid.uuid7())
        grp = fm.FindingGroup(engagement=eng, name="G", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        parent = fm.BoardFinding(
            engagement_id=eng.id, group_id=grp.id, order_index=0, title="Fleet vuln",
            severity=Severity.high,
            content_json={"description": schema.doc_from_text("d"), "remediation": schema.doc_from_text("r")},
        )
        db.add(parent)
        db.flush()
        child = fm.BoardFinding(
            engagement_id=eng.id, group_id=grp.id, parent_id=parent.id, order_index=1,
            title="Fleet vuln", severity=Severity.high, target_host="192.0.2.9", target_port="443",
            content_json={},
        )
        db.add(child)
        db.commit()
        return eng.id, parent.id, child.id


def test_suppress_include_remove_one_host(client, stub_host, session_factory):
    _, parent_id, child_id = _tree(session_factory)
    disp = f"{UI}/findings/{parent_id}/hosts/disposition"

    assert client.post(disp, data={"host_id": str(child_id), "action": "suppress"}).status_code == 302
    with session_factory() as db:
        assert db.get(fm.BoardFinding, child_id).include_in_report is False   # kept, dropped from report

    client.post(disp, data={"host_id": str(child_id), "action": "include"})
    with session_factory() as db:
        assert db.get(fm.BoardFinding, child_id).include_in_report is True

    client.post(disp, data={"host_id": str(child_id), "action": "remove"})
    with session_factory() as db:
        assert db.get(fm.BoardFinding, child_id) is None                      # false-positive: gone


def test_add_a_host(client, stub_host, session_factory):
    _, parent_id, _ = _tree(session_factory)
    resp = client.post(f"{UI}/findings/{parent_id}/hosts",
                       data={"target_host": "10.9.9.9", "target_port": "8443"})
    assert resp.status_code == 302
    with session_factory() as db:
        kids = db.query(fm.BoardFinding).filter_by(parent_id=parent_id).all()
        assert any(k.target_host == "10.9.9.9" and k.target_port == "8443" for k in kids)


def test_section_suppression_persists_via_the_editor_form(client, stub_host, session_factory):
    _, parent_id, _ = _tree(session_factory)
    resp = client.post(f"{UI}/findings/{parent_id}", data={
        "title": "Fleet vuln", "severity": "high", "include_in_report": "on",
        "suppress": ["remediation", "evidence"],
    })
    assert resp.status_code == 302
    with session_factory() as db:
        stored = set(db.get(fm.BoardFinding, parent_id).suppressed_sections or [])
    assert stored == {"remediation", "evidence"}


def test_bad_suppress_key_is_dropped(client, stub_host, session_factory):
    _, parent_id, _ = _tree(session_factory)
    client.post(f"{UI}/findings/{parent_id}", data={
        "title": "Fleet vuln", "severity": "high", "include_in_report": "on",
        "suppress": ["remediation", "not_a_section"],
    })
    with session_factory() as db:
        assert set(db.get(fm.BoardFinding, parent_id).suppressed_sections or []) == {"remediation"}


def test_editor_renders_hosts_table_and_suppress_controls(client, stub_host, session_factory):
    _, parent_id, _ = _tree(session_factory)
    body = client.get(f"{UI}/findings/{parent_id}").get_data(as_text=True)
    assert "Affected hosts" in body and "192.0.2.9" in body
    assert "Suppress sections from the report" in body
