"""The finding-detail retest UI (lotek#621/#622) — the browser write-path that records a verify-the-fix
round. `findings_service.record_retest` already owned the outcome→status policy; before this route there
was NO caller, so a retest round could never be entered and the report's remediation-status table stayed
empty. These tests drive scribble's own `bp` blueprint through the `stub_host` fixture (no lotek boot).
"""

from __future__ import annotations

import uuid
from datetime import date

import scribble.models as fm
from scribble.enums import FindingStatus, RetestOutcome

_MISSING_ID = uuid.uuid7()  # well-formed id that is not in the table

UI = "/scribble"


def _finding(session_factory):
    with session_factory() as db:
        eng = fm.Engagement(name="Retest Co", scope_type="external")
        db.add(eng)
        db.commit()
        tmpl = db.query(fm.VulnerabilityTemplate).first()
        finding = fm.EngagementFinding.from_template(tmpl, engagement_id=eng.id, order_index=0)
        db.add(finding)
        db.commit()
        return finding.id


def test_record_retest_remediated_transitions_status(client, stub_host, session_factory):
    fid = _finding(session_factory)
    resp = client.post(
        f"{UI}/findings/{fid}/retest",
        data={"outcome": "remediated", "tested_by": "QA", "tested_on": "2026-09-01",
              "notes": "patch verified"},
    )
    assert resp.status_code == 302
    with session_factory() as db:
        f = db.get(fm.EngagementFinding, fid)
        assert f.status == FindingStatus.fixed  # remediated -> fixed, via the ONE writer
        rounds = list(f.retests)
        assert len(rounds) == 1
        r = rounds[0]
        assert r.outcome == RetestOutcome.remediated
        assert r.tested_by == "QA"
        assert r.tested_on == date(2026, 9, 1)
        assert r.notes == "patch verified"


def test_record_retest_not_tested_leaves_status(client, stub_host, session_factory):
    fid = _finding(session_factory)
    with session_factory() as db:
        before = db.get(fm.EngagementFinding, fid).status

    resp = client.post(f"{UI}/findings/{fid}/retest", data={"outcome": "not_tested"})
    assert resp.status_code == 302
    with session_factory() as db:
        f = db.get(fm.EngagementFinding, fid)
        assert f.status == before  # not_tested records a round but is no verdict on the fix
        assert len(list(f.retests)) == 1


def test_record_retest_rejects_unknown_outcome(client, stub_host, session_factory):
    fid = _finding(session_factory)
    resp = client.post(f"{UI}/findings/{fid}/retest", data={"outcome": "totally-bogus"})
    assert resp.status_code == 400
    with session_factory() as db:
        assert list(db.get(fm.EngagementFinding, fid).retests) == []  # nothing recorded


def test_record_retest_missing_finding_404(client, stub_host):
    resp = client.post(f"{UI}/findings/{_MISSING_ID}/retest", data={"outcome": "remediated"})
    assert resp.status_code == 404


def test_record_retest_refused_for_viewer(client, stub_host, session_factory):
    fid = _finding(session_factory)
    stub_host.can_write_value = False
    resp = client.post(f"{UI}/findings/{fid}/retest", data={"outcome": "remediated"})
    assert resp.status_code == 403
    with session_factory() as db:
        assert list(db.get(fm.EngagementFinding, fid).retests) == []


def test_finding_page_renders_retest_card_and_history(client, stub_host, session_factory):
    fid = _finding(session_factory)
    # empty state before any round
    body = client.get(f"{UI}/findings/{fid}").get_data(as_text=True)
    assert "Verify the fix" in body
    assert "No retest recorded yet" in body
    assert f'action="/scribble/findings/{fid}/retest"' in body  # the writable form

    # record one, then it appears in the history table
    client.post(f"{UI}/findings/{fid}/retest",
                data={"outcome": "partially_remediated", "tested_by": "Ana"})
    body = client.get(f"{UI}/findings/{fid}").get_data(as_text=True)
    assert "partially remediated" in body  # humanized outcome label
    assert "Ana" in body


def test_finding_page_hides_retest_form_from_viewer(client, stub_host, session_factory):
    fid = _finding(session_factory)
    stub_host.can_write_value = False
    body = client.get(f"{UI}/findings/{fid}").get_data(as_text=True)
    assert "Verify the fix" in body          # history stays visible read-only
    assert "Record retest" not in body       # but the form is gated away
