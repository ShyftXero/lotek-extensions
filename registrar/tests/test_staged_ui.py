"""#140 — the human half of the staged-action workflow: a browser surface that lists pending staged
actions and approves/rejects them. The service gates (INV-EXT-02) were already correct; this proves the
dashboard reaches them, and that a mis-aimed request is still refused server-side (not just hidden).

The `client` fixture is an interactive admin who can write (conftest defaults). Staged rows are inserted
directly, mirroring tests/test_machine_api.py.
"""
from __future__ import annotations

import uuid

from registrar.models import AuditRecord, StagedAction

PREFIX = "/registrar"


def _stage(session_factory, *, initiator_id, engagement_id=None, verb="create_node"):
    with session_factory() as db:
        row = StagedAction(verb=verb, provider="null", args_json='{"name": "c2-1"}',
                           initiator_id=initiator_id, engagement_id=engagement_id, status="pending")
        db.add(row)
        db.commit()
        return row.id


def _latest_audit_result(session_factory):
    with session_factory() as db:
        row = db.query(AuditRecord).order_by(AuditRecord.at.desc()).first()
        return row.result if row else None


def _status(session_factory, staged_id):
    with session_factory() as db:
        return db.get(StagedAction, staged_id).status


# ── the dashboard now shows staged actions ──────────────────────────────────────────────────────────

def test_dashboard_lists_a_staged_action_with_approve_and_reject(client, session_factory):
    sid = _stage(session_factory, initiator_id=uuid.uuid7())  # someone else staged it
    body = client.get(f"{PREFIX}/").get_data(as_text=True)
    assert "Staged actions" in body
    assert f"{PREFIX}/staged/{sid}/approve" in body
    assert f"{PREFIX}/staged/{sid}/reject" in body


def test_initiator_sees_their_own_row_but_no_approve_button(client, hooks, session_factory):
    sid = _stage(session_factory, initiator_id=hooks["actor"].id)  # the current actor staged it
    body = client.get(f"{PREFIX}/").get_data(as_text=True)
    assert "you staged this" in body
    assert f"{PREFIX}/staged/{sid}/approve" not in body  # cannot approve own
    assert f"{PREFIX}/staged/{sid}/reject" in body        # may still cancel it


# ── approve / reject work from the browser and are audited ───────────────────────────────────────────

def test_approve_executes_and_audits(client, session_factory):
    sid = _stage(session_factory, initiator_id=uuid.uuid7())
    resp = client.post(f"{PREFIX}/staged/{sid}/approve")
    assert resp.status_code in (302, 303)
    assert _status(session_factory, sid) == "executed"
    assert _latest_audit_result(session_factory) == "executed"


def test_reject_declines_without_executing_and_audits(client, session_factory):
    sid = _stage(session_factory, initiator_id=uuid.uuid7())
    resp = client.post(f"{PREFIX}/staged/{sid}/reject")
    assert resp.status_code in (302, 303)
    assert _status(session_factory, sid) == "rejected"
    assert _latest_audit_result(session_factory) == "rejected"


# ── the server-side gate still refuses, even when the UI would have hidden the control ────────────────

def test_initiator_cannot_approve_own_via_the_route(client, hooks, session_factory):
    sid = _stage(session_factory, initiator_id=hooks["actor"].id)
    client.post(f"{PREFIX}/staged/{sid}/approve")
    # two-person rule must hold at the route, not just in the UI
    assert _status(session_factory, sid) == "pending"


def test_a_non_interactive_session_cannot_approve(client, hooks, session_factory):
    sid = _stage(session_factory, initiator_id=uuid.uuid7())
    hooks["is_interactive"] = False
    client.post(f"{PREFIX}/staged/{sid}/approve")
    assert _status(session_factory, sid) == "pending"


def test_a_viewer_cannot_approve(client, hooks, session_factory):
    sid = _stage(session_factory, initiator_id=uuid.uuid7())
    hooks["can_write"] = False
    client.post(f"{PREFIX}/staged/{sid}/approve")
    assert _status(session_factory, sid) == "pending"


# ── tenancy: a staged row the actor cannot see is not actionable by guessing its id ───────────────────

def test_approve_404s_for_a_staged_row_outside_the_actors_scope(client, hooks, session_factory):
    other_engagement = uuid.uuid7()
    sid = _stage(session_factory, initiator_id=uuid.uuid7(), engagement_id=other_engagement)
    hooks["actor"].role.value = "operator"          # not admin
    hooks["visible_engagement_ids"] = {uuid.uuid7()}  # some OTHER engagement
    resp = client.post(f"{PREFIX}/staged/{sid}/approve")
    assert resp.status_code == 404
    assert _status(session_factory, sid) == "pending"
