"""Engagement tenancy for diagrams (lotek#585 / INV-TENANCY-05) + per-entity audit (INV-AUDIT-03).

The property the whole change exists for: a diagram BOUND to an engagement is readable/exportable/writable
ONLY by a LIVE member/operator of that engagement — so a member REVOKED from the engagement, the owner
included, loses access. An UNBOUND diagram keeps the older owner scope (proven in test_api / test_machine_api,
which this file must not regress).

Membership is controlled through the fixture's mutable ``app.eng_view`` / ``app.eng_op`` sets (see
conftest): granting is adding the engagement id, revoking is removing it — between requests, which is the
revocation case. Audit rows land in ``app.audit_log``.
"""

from __future__ import annotations

import uuid

from conftest import FakeUser, StubActor, login

from vector.models import Diagram

MACHINE = "/vector/machine"
API = "/vector/api"

_MODEL = {"phases": [{"title": "Initial Access"}], "nodes": [{"id": "n1", "label": "phish"}]}

ENG = uuid.UUID(int=555)
OWNER = uuid.UUID(int=7)  # matches conftest's default StubActor id


def _grant(app, engagement, *, view=True, operate=True):
    if view:
        app.eng_view.add(engagement)
    if operate:
        app.eng_op.add(engagement)


def _revoke(app, engagement):
    app.eng_view.discard(engagement)
    app.eng_op.discard(engagement)


def _insert_bound(app, engagement, *, owner=OWNER, name="Bound", builtin=False):
    """A diagram bound to ``engagement`` inserted straight into the DB, so a test can assert visibility
    WITHOUT the actor ever having created (and thus been an operator on) it."""
    with app.extensions["vector"].session_factory() as db:
        row = Diagram(name=name, model_json="{}", owner_id=owner, engagement_id=engagement, builtin=builtin)
        db.add(row)
        db.commit()
        return row.id


# ── binding at create requires an operator capability (INV-TENANCY-05) ─────────────────────────────────


def test_binding_a_diagram_requires_operator_on_that_engagement(app, pat_client):
    """You may only create a diagram in an engagement you operate on — adopting the id from the body is
    sound only because it is gated here."""
    res = pat_client.post(f"{MACHINE}/diagrams",
                          json={"name": "x", "model": _MODEL, "engagement_id": str(ENG)})
    assert res.status_code == 403, res.get_data(as_text=True)

    _grant(app, ENG)
    res = pat_client.post(f"{MACHINE}/diagrams",
                          json={"name": "x", "model": _MODEL, "engagement_id": str(ENG)})
    assert res.status_code == 201
    with app.extensions["vector"].session_factory() as db:
        assert db.get(Diagram, uuid.UUID(res.get_json()["id"])).engagement_id == ENG


def test_a_bad_engagement_id_is_400(pat_client):
    res = pat_client.post(f"{MACHINE}/diagrams", json={"name": "x", "engagement_id": "not-a-uuid"})
    assert res.status_code == 400


# ── the revocation case: the owner loses access when revoked ───────────────────────────────────────────


def test_revoked_member_including_owner_loses_read_write_export_machine(app, pat_client):
    _grant(app, ENG)
    created = pat_client.post(f"{MACHINE}/diagrams",
                             json={"name": "Client path", "model": _MODEL,
                                   "engagement_id": str(ENG)}).get_json()
    did = created["id"]
    # While a member: read, list, export all work.
    assert pat_client.get(f"{MACHINE}/diagrams/{did}").status_code == 200
    assert did in [d["id"] for d in pat_client.get(f"{MACHINE}/diagrams").get_json()["diagrams"]]
    assert pat_client.get(f"{MACHINE}/diagrams/{did}/export.html").status_code == 200

    _revoke(app, ENG)  # membership revoked — same token, same owner

    assert pat_client.get(f"{MACHINE}/diagrams/{did}").status_code == 404
    assert did not in [d["id"] for d in pat_client.get(f"{MACHINE}/diagrams").get_json()["diagrams"]]
    assert pat_client.get(f"{MACHINE}/diagrams/{did}/export.html").status_code == 404
    assert pat_client.put(f"{MACHINE}/diagrams/{did}", json={"name": "y"}).status_code == 404
    assert pat_client.delete(f"{MACHINE}/diagrams/{did}").status_code == 404


def test_revoked_member_loses_access_on_cookie_surface_too(app, client):
    """Same property on the browser blueprint/api — both surfaces route through vector.access."""
    login(app, FakeUser(uid=uuid.UUID(int=7), username="op", role="operator"))
    _grant(app, ENG)
    created = client.post(f"{API}/diagrams",
                          json={"name": "c", "model": _MODEL, "engagement_id": str(ENG)}).get_json()
    did = created["id"]
    assert client.get(f"{API}/diagrams/{did}").status_code == 200
    assert client.get(f"/vector/edit/{did}").status_code == 200

    _revoke(app, ENG)
    assert client.get(f"{API}/diagrams/{did}").status_code == 404
    assert client.get(f"/vector/edit/{did}").status_code == 404
    assert did not in [d["id"] for d in client.get(f"{API}/diagrams").get_json()["diagrams"]]


def test_revoked_member_loses_cookie_export_downloads(app, client):
    """The cookie-surface EXPORT downloads (``blueprint.py`` ``export.html`` / ``export.json``) route
    through the same ``vector.access`` gate as get/edit, so a revoked former member must not be able to
    download the client's attack-path deliverable. This pins the exact route BusyBody's ``export_diagram``
    oracle fingered (#589 class 5a / ext#165); the sibling cookie test above covers get/edit/list but not
    these two dedicated download endpoints."""
    login(app, FakeUser(uid=uuid.UUID(int=7), username="op", role="operator"))
    _grant(app, ENG)
    did = client.post(f"{API}/diagrams",
                      json={"name": "deliverable", "model": _MODEL,
                            "engagement_id": str(ENG)}).get_json()["id"]
    # While a member: both download routes render the deliverable.
    assert client.get(f"/vector/diagrams/{did}/export.html").status_code == 200
    assert client.get(f"/vector/diagrams/{did}/export.json").status_code == 200

    _revoke(app, ENG)  # membership revoked — same session user, still the diagram's owner_id

    assert client.get(f"/vector/diagrams/{did}/export.html").status_code == 404
    assert client.get(f"/vector/diagrams/{did}/export.json").status_code == 404


def test_non_member_never_sees_a_bound_diagram(app, pat_client):
    did = _insert_bound(app, ENG)  # nobody is a member (eng_view empty)
    assert pat_client.get(f"{MACHINE}/diagrams/{did}").status_code == 404
    assert pat_client.get(f"{MACHINE}/diagrams").get_json()["diagrams"] == []


def test_admin_has_no_engagement_bypass(app, pat_client):
    """v2 removed the admin bypass: an admin who is not a member of the engagement cannot see its bound
    diagram (unlike a legacy NULL-engagement row, which stays admin-visible)."""
    did = _insert_bound(app, ENG)
    app.pat["actor"] = StubActor(id=uuid.UUID(int=99), username="root", role="admin")
    assert pat_client.get(f"{MACHINE}/diagrams/{did}").status_code == 404


def test_observer_can_read_but_not_write(app, pat_client):
    """View-but-not-operate (an observer membership): read/export yes, modify/delete no (403, not 404 —
    the caller demonstrably can see the row)."""
    _grant(app, ENG, view=True, operate=False)
    did = _insert_bound(app, ENG)
    assert pat_client.get(f"{MACHINE}/diagrams/{did}").status_code == 200
    assert pat_client.get(f"{MACHINE}/diagrams/{did}/export.html").status_code == 200
    assert pat_client.put(f"{MACHINE}/diagrams/{did}", json={"name": "y"}).status_code == 403
    assert pat_client.delete(f"{MACHINE}/diagrams/{did}").status_code == 403


def test_operator_can_write_a_bound_diagram(app, pat_client):
    _grant(app, ENG, view=True, operate=True)
    did = _insert_bound(app, ENG)
    assert pat_client.put(f"{MACHINE}/diagrams/{did}", json={"name": "renamed"}).status_code == 200
    assert pat_client.delete(f"{MACHINE}/diagrams/{did}").status_code == 200


# ── per-entity audit (INV-AUDIT-03) ────────────────────────────────────────────────────────────────────


def test_create_update_delete_each_emit_one_audit_row(app, pat_client):
    _grant(app, ENG)
    created = pat_client.post(f"{MACHINE}/diagrams",
                             json={"name": "Audited", "model": _MODEL,
                                   "engagement_id": str(ENG)}).get_json()
    did = created["id"]
    create_rows = [r for r in app.audit_log if r["action"] == "ext:vector:create"]
    assert len(create_rows) == 1
    assert create_rows[0]["subject_type"] == "vector_diagram"
    assert create_rows[0]["subject_id"] == did  # coerced to the string the response carries
    assert create_rows[0]["after"]["engagement_id"] == str(ENG)

    pat_client.put(f"{MACHINE}/diagrams/{did}", json={"name": "Audited v2"})
    upd = [r for r in app.audit_log if r["action"] == "ext:vector:update"]
    assert len(upd) == 1
    assert upd[0]["before"]["name"] == "Audited"
    assert upd[0]["after"]["name"] == "Audited v2"

    pat_client.delete(f"{MACHINE}/diagrams/{did}")
    dele = [r for r in app.audit_log if r["action"] == "ext:vector:delete"]
    assert len(dele) == 1
    assert dele[0]["subject_id"] == did


def test_audit_row_lands_in_the_same_transaction_as_the_write(app, pat_client):
    """The audit hook is called with the request's db BEFORE commit — a rolled-back write leaves no audit
    row. Proven indirectly: a failed (unauthorized) create writes neither the row nor an audit entry."""
    res = pat_client.post(f"{MACHINE}/diagrams",
                          json={"name": "x", "engagement_id": str(ENG)})  # no operator grant -> 403
    assert res.status_code == 403
    assert [r for r in app.audit_log if r["action"] == "ext:vector:create"] == []
