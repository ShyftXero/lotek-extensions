"""The PAT machine API (``registrar/api_pat.py``) — scope gating, PAT-actor attribution, and the
confirm-tier line that keeps outward effects human-approved.

These are Registrar's OWN proofs. The host's token/scope *scheme* is lotek's concern (proven there against
the real authenticator); what must be proven HERE is that Registrar declares the right scope on every
machine route, stages confirm-tier verbs instead of running them, attributes the staged row to the PAT
principal, and exposes no approval route at all.

INV-EXT-02 is defended twice on purpose: the route is absent from this surface, AND ``service.approve``
itself refuses a non-interactive caller. A missing route is a surface decision that a later commit could
undo; the service guard is the one that cannot be routed around.
"""

from __future__ import annotations

import uuid

import pytest

from registrar.enums import ServerKind, ServerState, Tier
from registrar.host import SCOPE_ATTR
from registrar.models import Server, StagedAction
from registrar.service import ApprovalDenied, approve, tier_of

MACHINE = "/registrar/machine"


def _machine_rules(app):
    return [r for r in app.url_map.iter_rules() if str(r.rule).startswith(MACHINE)]


# ── the load-bearing invariants ───────────────────────────────────────────────────────────────────────


def test_every_machine_route_is_scope_gated(app):
    """No route on this surface may be reachable by a merely-authenticated token.

    Walks the real ``url_map`` rather than a hand-kept list, so a route added later without
    ``@host.require_scope`` fails this test instead of shipping ungated.
    """
    rules = _machine_rules(app)
    assert rules, "no /registrar/machine routes are registered at all"
    ungated = [
        str(r.rule) for r in rules
        if not hasattr(app.view_functions[r.endpoint], SCOPE_ATTR)
    ]
    assert ungated == [], f"machine routes missing require_scope: {ungated}"


def test_there_is_no_approve_route_on_the_machine_surface(app):
    """INV-EXT-02 at the surface: a PAT stages, a human approves. Executing a staged action needs an
    interactive session by a different user, so the route simply does not exist here."""
    offenders = [str(r.rule) for r in _machine_rules(app) if "approve" in str(r.rule)]
    assert offenders == [], f"machine surface exposes staged-action approval: {offenders}"


def test_the_browser_surface_still_has_approve(app):
    """Control for the test above: approval exists on the cookie-authed surface, so its absence from the
    machine surface is a deliberate omission and not a typo in the assertion."""
    browser = [str(r.rule) for r in app.url_map.iter_rules() if str(r.rule).startswith("/registrar/api")]
    assert any("approve" in r for r in browser)


def test_approve_itself_refuses_a_non_interactive_caller(session_factory, hooks):
    """The guard that cannot be routed around. Even holding a staged row, a machine principal cannot
    execute it: ``service.approve`` demands an interactive dashboard session."""
    with session_factory() as db:
        staged = StagedAction(verb="create_node", provider="null", args_json="{}",
                              initiator_id=uuid.uuid7(), status="pending")
        db.add(staged)
        db.commit()
        with pytest.raises(ApprovalDenied, match="interactive"):
            approve(db, staged, confirmer_id=hooks["pat_actor"].id, confirmer_name="agent",
                    is_interactive=False, can_write=True)
        assert staged.status == "pending", "a refused approval must not advance the action"


def test_approve_refuses_the_initiator_as_their_own_confirmer(session_factory):
    """Two-person rule: staging and approving cannot be the same principal."""
    initiator = uuid.uuid7()
    with session_factory() as db:
        staged = StagedAction(verb="create_node", provider="null", args_json="{}",
                              initiator_id=initiator, status="pending")
        db.add(staged)
        db.commit()
        with pytest.raises(ApprovalDenied, match="different user"):
            approve(db, staged, confirmer_id=initiator, confirmer_name="same",
                    is_interactive=True, can_write=True)
        assert staged.status == "pending"


# ── scope gating ─────────────────────────────────────────────────────────────────────────────────────


def test_read_token_cannot_act(pat_client, hooks, session_factory):
    """A read-scoped token is refused by the write route, and stages nothing."""
    hooks["pat_actor"] = type(hooks["pat_actor"])(scopes=frozenset({"read"}))
    res = pat_client.post(f"{MACHINE}/action", json={"verb": "list_nodes"})
    assert res.status_code == 403, res.get_data(as_text=True)
    with session_factory() as db:
        assert db.query(StagedAction).count() == 0


def test_read_token_can_read(pat_client):
    for path in ("/servers", "/domains", "/staged", "/audit"):
        res = pat_client.get(f"{MACHINE}{path}")
        assert res.status_code == 200, f"{path}: {res.get_data(as_text=True)}"


def test_unauthenticated_token_is_refused(pat_client, hooks):
    """The host's ``pat_authenticate`` result is honoured as-is by the blueprint's before_request."""
    hooks["pat_authenticate"] = ({"error": "unauthorized"}, 401)
    assert pat_client.get(f"{MACHINE}/servers").status_code == 401


def test_fails_closed_when_no_host_injected_the_pat_hooks(app, client):
    """Unmounted (or a host that injected nothing) must be a 503, never an open door."""
    app.extensions["registrar"].extras.pop("pat_authenticate")
    res = client.get(f"{MACHINE}/servers")
    assert res.status_code == 503
    assert res.get_json()["error"] == "unavailable"


# ── the tier gate: direct runs, confirm stages ────────────────────────────────────────────────────────


def test_direct_tier_verb_runs_inline(pat_client):
    res = pat_client.post(f"{MACHINE}/action", json={"verb": "list_nodes", "provider": "null"})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json()["status"] == "executed"


def test_confirm_tier_verb_is_staged_not_executed(pat_client, hooks, session_factory):
    """The whole point of the surface: an outward-effect verb becomes a pending row awaiting a human."""
    actor = hooks["pat_actor"]
    res = pat_client.post(f"{MACHINE}/action",
                          json={"verb": "create_node", "provider": "null", "args": {"name": "c2-1"}})
    assert res.status_code == 202, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "staged"
    with session_factory() as db:
        row = db.get(StagedAction, uuid.UUID(body["staged_id"]))
        assert row.status == "pending"
        assert row.verb == "create_node"
        # Attribution comes from the PAT principal — `current_actor_id()` is None on this surface, and a
        # NULL initiator would make the two-person approval rule unenforceable.
        assert row.initiator_id == actor.id
        assert row.initiator_id is not None


def test_an_unknown_verb_is_staged_never_executed(pat_client, session_factory):
    """``tier_of`` defaults to confirm-tier, so an unrecognized verb fails CLOSED (staged for a human)
    rather than being dispatched."""
    assert tier_of("definitely_not_a_verb") is Tier.confirm
    res = pat_client.post(f"{MACHINE}/action", json={"verb": "definitely_not_a_verb"})
    assert res.status_code == 202
    with session_factory() as db:
        assert db.query(StagedAction).count() == 1


def test_action_requires_a_verb(pat_client):
    assert pat_client.post(f"{MACHINE}/action", json={}).status_code == 400


def test_staging_writes_a_core_audit_event_through_the_host_seam(pat_client, audit_log):
    """INV-AUDIT-03: the local audit row is not the only record — the host's audited-write seam is called
    in the same transaction, so a staged action is defensible outside the extension's own tables."""
    pat_client.post(f"{MACHINE}/action", json={"verb": "send_sms", "args": {"to": "x", "body": "y"}})
    assert "ext:registrar:staged" in audit_log.actions()


def test_staged_list_shows_the_pending_action(pat_client):
    pat_client.post(f"{MACHINE}/action", json={"verb": "create_node", "args": {"name": "c2-1"}})
    staged = pat_client.get(f"{MACHINE}/staged").get_json()["staged"]
    assert len(staged) == 1
    assert staged[0]["verb"] == "create_node"


def test_audit_read_surfaces_the_staged_record(pat_client):
    pat_client.post(f"{MACHINE}/action", json={"verb": "create_node", "args": {"name": "c2-1"}})
    audit = pat_client.get(f"{MACHINE}/audit").get_json()["audit"]
    assert any(a["verb"] == "create_node" and a["result"] == "staged" for a in audit)


def test_the_sms_audit_detail_carries_no_recipient_or_body(pat_client):
    """INV-SECRET-05: the audit projection is allow-listed, so staging an SMS records that it happened
    without storing who it was to or what it said."""
    pat_client.post(f"{MACHINE}/action",
                    json={"verb": "send_sms", "args": {"to": "+15551234567", "body": "secret"}})
    audit = pat_client.get(f"{MACHINE}/audit").get_json()["audit"]
    details = " ".join(a["detail"] or "" for a in audit)
    assert "+15551234567" not in details
    assert "secret" not in details


# ── read scoping ─────────────────────────────────────────────────────────────────────────────────────


def _add_server(session_factory, *, kind, engagement_id=None, name="srv"):
    with session_factory() as db:
        row = Server(kind=kind, state=ServerState.planned, name=name, provider="null",
                     engagement_id=engagement_id)
        db.add(row)
        db.commit()
        return row.id


def test_transient_servers_are_scoped_to_the_tokens_engagements(pat_client, hooks, session_factory):
    mine, theirs = uuid.uuid7(), uuid.uuid7()
    _add_server(session_factory, kind=ServerKind.transient, engagement_id=mine, name="mine")
    _add_server(session_factory, kind=ServerKind.transient, engagement_id=theirs, name="theirs")
    hooks["visible_engagement_ids"] = {mine}
    names = [s["name"] for s in pat_client.get(f"{MACHINE}/servers").get_json()["servers"]]
    assert names == ["mine"]


def test_static_infra_is_admin_only(pat_client, hooks, session_factory):
    """B2c: org (static) infrastructure is not engagement-scoped, so a non-admin token must not see it."""
    _add_server(session_factory, kind=ServerKind.static, name="jump-box")
    hooks["visible_engagement_ids"] = set()

    hooks["pat_actor"] = type(hooks["pat_actor"])(role="operator")
    assert pat_client.get(f"{MACHINE}/servers").get_json()["servers"] == []

    hooks["pat_actor"] = type(hooks["pat_actor"])(role="admin")
    names = [s["name"] for s in pat_client.get(f"{MACHINE}/servers").get_json()["servers"]]
    assert names == ["jump-box"]
