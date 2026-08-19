"""INV-TENANCY-06 — registrar list endpoints must not leak across engagements.

Before this fix, ``/domains``, ``/staged`` and ``/audit`` returned EVERY row with no engagement
filter, so a caller holding membership only in engagement A could read engagement B's domains, staged
actions, and the org-wide audit trail. These tests pin the scoped contract on all three surfaces (the
service helpers, the cookie-authed human API, and the PAT machine mirror) and on the shared rule that
an unowned/unbound row is org-level inventory -> admin-only, mirroring ``visible_servers``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from registrar.models import AuditRecord, Domain, StagedAction
from registrar.service import visible_domains, visible_staged

API = "/registrar/api"
MACHINE = "/registrar/machine"


# ── fixtures / helpers ───────────────────────────────────────────────────────────────────────────────


def _domain(session_factory, name, *, checked_out_to=None):
    with session_factory() as db:
        db.add(Domain(name=name, provider="null", registered=True, checked_out_to=checked_out_to))
        db.commit()


def _staged(session_factory, verb, *, engagement_id=None):
    with session_factory() as db:
        row = StagedAction(verb=verb, provider="null", args_json="{}", engagement_id=engagement_id,
                           initiator_id=uuid.uuid7(), status="pending", created_at=datetime.now(UTC))
        db.add(row)
        db.commit()
        return row.id


def _audit(session_factory, verb):
    with session_factory() as db:
        db.add(AuditRecord(at=datetime.now(UTC), actor="x", verb=verb, provider="null",
                           tier="direct", detail="", result="executed"))
        db.commit()


def _operator(hooks, visible):
    """Shape the BROWSER session as a non-admin operator scoped to ``visible`` engagements."""
    from tests.conftest import FakeUser
    u = FakeUser(role="operator")
    hooks["actor"] = u
    hooks["visible_engagement_ids"] = set(visible)
    return u


# ── the shared service rule ──────────────────────────────────────────────────────────────────────────


def test_visible_domains_scopes_by_checkout(session_factory):
    mine, theirs = uuid.uuid7(), uuid.uuid7()
    _domain(session_factory, "mine.example", checked_out_to=mine)
    _domain(session_factory, "theirs.example", checked_out_to=theirs)
    _domain(session_factory, "pool.example", checked_out_to=None)  # unowned org inventory
    with session_factory() as db:
        # operator scoped to {mine}: sees only its checked-out domain, NOT theirs, NOT the org pool
        names = [d.name for d in visible_domains(db, visible_engagement_ids={mine}, is_admin=False)]
        assert names == ["mine.example"]
        # admin: sees the unowned org pool too (but still not another engagement's — via visible set)
        admin_names = [d.name for d in visible_domains(db, visible_engagement_ids={mine}, is_admin=True)]
        assert "pool.example" in admin_names and "mine.example" in admin_names
        assert "theirs.example" not in admin_names
        # standalone (None): sees everything
        alln = [d.name for d in visible_domains(db, visible_engagement_ids=None, is_admin=False)]
        assert set(alln) == {"mine.example", "theirs.example", "pool.example"}


def test_visible_staged_scopes_by_engagement(session_factory):
    mine, theirs = uuid.uuid7(), uuid.uuid7()
    _staged(session_factory, "create_node", engagement_id=mine)
    _staged(session_factory, "destroy_node", engagement_id=theirs)
    _staged(session_factory, "register_domain", engagement_id=None)  # org-level / unbound
    with session_factory() as db:
        verbs = [s.verb for s in visible_staged(db, visible_engagement_ids={mine}, is_admin=False)]
        assert verbs == ["create_node"]
        admin_verbs = {s.verb for s in visible_staged(db, visible_engagement_ids={mine}, is_admin=True)}
        assert "register_domain" in admin_verbs and "create_node" in admin_verbs
        assert "destroy_node" not in admin_verbs


# ── human (cookie) API ───────────────────────────────────────────────────────────────────────────────


def test_human_domains_hide_another_engagements_checkout(client, hooks, session_factory):
    mine, theirs = uuid.uuid7(), uuid.uuid7()
    _domain(session_factory, "mine.example", checked_out_to=mine)
    _domain(session_factory, "theirs.example", checked_out_to=theirs)
    _operator(hooks, {mine})
    names = [d["name"] for d in client.get(f"{API}/domains").get_json()["domains"]]
    assert names == ["mine.example"], "a non-member must not see another engagement's domain"


def test_human_staged_hide_another_engagements_actions(client, hooks, session_factory):
    mine, theirs = uuid.uuid7(), uuid.uuid7()
    _staged(session_factory, "create_node", engagement_id=mine)
    _staged(session_factory, "destroy_node", engagement_id=theirs)
    _operator(hooks, {mine})
    verbs = [s["verb"] for s in client.get(f"{API}/staged").get_json()["staged"]]
    assert verbs == ["create_node"]


def test_human_audit_is_admin_only(client, hooks, session_factory):
    _audit(session_factory, "create_node")
    _operator(hooks, set())
    assert client.get(f"{API}/audit").get_json()["audit"] == [], "audit must be hidden from a non-admin"

    from tests.conftest import FakeUser
    hooks["actor"] = FakeUser(role="admin")
    verbs = [a["verb"] for a in client.get(f"{API}/audit").get_json()["audit"]]
    assert verbs == ["create_node"]


def test_human_audit_visible_standalone(app, client, session_factory):
    """Standalone REGISTRAR (no host ``current_actor`` hook at all) is a single local user -> counts as
    admin -> sees its own trail. (A hook that is PRESENT but returns None is a mounted no-session caller,
    which is NOT admin — the audit stays hidden there, proven by ``test_human_audit_is_admin_only``.)"""
    _audit(session_factory, "list_nodes")
    app.extensions["registrar"].extras.pop("current_actor", None)
    app.extensions["registrar"].extras.pop("visible_engagement_ids", None)
    verbs = [a["verb"] for a in client.get(f"{API}/audit").get_json()["audit"]]
    assert verbs == ["list_nodes"]


# ── PAT machine API ──────────────────────────────────────────────────────────────────────────────────


def test_machine_domains_scoped_to_the_tokens_engagements(pat_client, hooks, session_factory):
    mine, theirs = uuid.uuid7(), uuid.uuid7()
    _domain(session_factory, "mine.example", checked_out_to=mine)
    _domain(session_factory, "theirs.example", checked_out_to=theirs)
    hooks["visible_engagement_ids"] = {mine}
    hooks["pat_actor"] = type(hooks["pat_actor"])(role="operator")
    names = [d["name"] for d in pat_client.get(f"{MACHINE}/domains").get_json()["domains"]]
    assert names == ["mine.example"]


def test_machine_staged_scoped_to_the_tokens_engagements(pat_client, hooks, session_factory):
    mine, theirs = uuid.uuid7(), uuid.uuid7()
    _staged(session_factory, "create_node", engagement_id=mine)
    _staged(session_factory, "destroy_node", engagement_id=theirs)
    hooks["visible_engagement_ids"] = {mine}
    hooks["pat_actor"] = type(hooks["pat_actor"])(role="operator")
    verbs = [s["verb"] for s in pat_client.get(f"{MACHINE}/staged").get_json()["staged"]]
    assert verbs == ["create_node"]


def test_machine_audit_is_admin_only(pat_client, hooks, session_factory):
    _audit(session_factory, "create_node")
    hooks["visible_engagement_ids"] = set()
    hooks["pat_actor"] = type(hooks["pat_actor"])(role="operator")
    assert pat_client.get(f"{MACHINE}/audit").get_json()["audit"] == []

    hooks["pat_actor"] = type(hooks["pat_actor"])(role="admin")
    verbs = [a["verb"] for a in pat_client.get(f"{MACHINE}/audit").get_json()["audit"]]
    assert verbs == ["create_node"]
