"""ext#139 — a confirm-tier action staged through the REAL route carries its engagement.

The sibling ``test_read_scoping.py`` is correct but every row it builds is hand-bound
(``engagement_id=mine``). Production never produced that shape: neither ``/action`` call site passed
``engagement_id`` to ``stage()``, so every staged row was unbound, which (a) hid it from the very
operator who staged it and (b) made ``approve()``'s confirm-time operator re-check dead code — the arm
``if eng is not None and can_operate_on is not None`` never ran.

These tests stage through the actual HTTP route, so they fail if the plumbing regresses: the row would
go back to unbound and the operator re-check would silently stop firing.
"""

from __future__ import annotations

import uuid

from registrar.models import StagedAction

API = "/registrar/api"
MACHINE = "/registrar/machine"


def _staged_rows(session_factory):
    with session_factory() as db:
        return db.query(StagedAction).all()


def _operator(hooks, *, visible=None, can_operate=True):
    from tests.conftest import FakeUser
    u = FakeUser(role="operator")
    hooks["actor"] = u
    hooks["visible_engagement_ids"] = set(visible) if visible is not None else None
    hooks["can_operate_on"] = (lambda _e: True) if can_operate is True else can_operate
    return u


# ── binding: the real route now records the engagement ────────────────────────────────────────────────


def test_cookie_action_binds_declared_engagement(client, hooks, session_factory):
    eng = uuid.uuid7()
    _operator(hooks, visible={eng})
    resp = client.post(f"{API}/action", json={"verb": "create_node", "engagement_id": str(eng),
                                              "args": {"name": "c2-1"}})
    assert resp.status_code == 202
    rows = _staged_rows(session_factory)
    assert len(rows) == 1 and rows[0].engagement_id == eng, "the staged row must carry the engagement"
    # and the operator who staged it can see it (the symptom: unbound rows were admin-only, hiding it)
    verbs = [s["verb"] for s in client.get(f"{API}/staged").get_json()["staged"]]
    assert verbs == ["create_node"]


def test_machine_action_binds_declared_engagement(pat_client, hooks, session_factory):
    eng = uuid.uuid7()
    hooks["can_operate_on"] = lambda _e: True
    resp = pat_client.post(f"{MACHINE}/action", json={"verb": "create_node", "engagement_id": str(eng),
                                                      "args": {"name": "c2-1"}})
    assert resp.status_code == 202
    rows = _staged_rows(session_factory)
    assert len(rows) == 1 and rows[0].engagement_id == eng


# ── stage-time authz: the initiator must be an operator on the engagement (INV-TENANCY-05) ─────────────


def test_cannot_stage_for_an_engagement_you_cannot_operate_on(client, hooks):
    eng = uuid.uuid7()
    _operator(hooks, can_operate=lambda _e: False)
    resp = client.post(f"{API}/action", json={"verb": "create_node", "engagement_id": str(eng)})
    assert resp.status_code == 403


def test_malformed_engagement_id_is_rejected(client, hooks):
    _operator(hooks)
    resp = client.post(f"{API}/action", json={"verb": "create_node", "engagement_id": "not-a-uuid"})
    assert resp.status_code == 400


# ── the confirm-time operator re-check is now LIVE (INV-EXT-02, the invariant's Red path) ──────────────


def _stage_as(client, hooks, eng, verb="destroy_node"):
    """Stage an engagement-bound action as initiator A (an operator on ``eng``). Returns staged id."""
    from tests.conftest import FakeUser
    hooks["actor"] = FakeUser(username="initiator", role="operator")
    hooks["can_operate_on"] = lambda _e: True
    resp = client.post(f"{API}/action", json={"verb": verb, "engagement_id": str(eng),
                                              "args": {"provider_ref": "null-x"}})
    assert resp.status_code == 202
    return resp.get_json()["staged_id"]


def test_approve_denied_when_confirmer_is_not_an_operator_on_the_engagement(client, hooks):
    eng = uuid.uuid7()
    sid = _stage_as(client, hooks, eng)
    # a DIFFERENT confirmer who holds NO operator capability on eng — the arm that used to be dead
    from tests.conftest import FakeUser
    hooks["actor"] = FakeUser(username="confirmer", role="operator")
    hooks["can_operate_on"] = lambda _e: False
    resp = client.post(f"{API}/staged/{sid}/approve")
    assert resp.status_code == 403, "confirm-time operator re-check must refuse a non-operator confirmer"


def test_approve_succeeds_when_confirmer_is_an_operator_on_the_engagement(client, hooks, session_factory):
    eng = uuid.uuid7()
    sid = _stage_as(client, hooks, eng)
    from tests.conftest import FakeUser
    hooks["actor"] = FakeUser(username="confirmer", role="operator")
    hooks["can_operate_on"] = lambda _e: True  # this confirmer IS an operator on eng
    resp = client.post(f"{API}/staged/{sid}/approve")
    assert resp.status_code == 200
    with session_factory() as db:
        assert db.get(StagedAction, uuid.UUID(sid)).status == "executed"
