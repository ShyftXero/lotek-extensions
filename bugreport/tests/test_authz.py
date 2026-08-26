"""The guard this extension exists to get right: **one user must not reach another user's report.**

"Users CRUD their own reports, admins CRUD all" is an IDOR surface — a guessable id is the whole attack.
So every case below hands user B a *real, valid* id belonging to user A and asserts B is refused on
BOTH surfaces (browser + PAT), and — separately — that A's row was not modified. A route that returns
403 but has already written is a pass on the status code and a failure on the thing that matters.

Two properties are pinned deliberately:

* **404, not 403** for another user's report (INV-TENANCY-01: "not authorized" and "does not exist" must
  be indistinguishable, or the surface is an existence oracle).
* the refusal comes from ONE shared predicate (``bugreport.service``), which is why neutralising
  ``visible_reports``/``load_visible`` turns BOTH surfaces red at once — see the red-then-green
  transcript in the PR body.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import FakeUser, StubActor, file_report, load, loaded

from bugreport.models import ReportStatus
from bugreport.service import load_visible, visible_reports

# ── the shared predicate, unit level ─────────────────────────────────────────────


def test_visible_reports_scopes_to_the_owner(app, hooks, client, session_factory):
    alice = hooks["actor"]
    file_report(client, title="alice's bug")
    bob = FakeUser(username="bob")
    hooks["actor"] = bob
    file_report(client, title="bob's bug")

    with session_factory() as db:
        assert [r.title for r in visible_reports(db, actor_id=alice.id, is_admin=False)] == ["alice's bug"]
        assert [r.title for r in visible_reports(db, actor_id=bob.id, is_admin=False)] == ["bob's bug"]
        assert len(visible_reports(db, actor_id=bob.id, is_admin=True)) == 2
        # Anonymous: no id, not admin -> nothing. NOT "everything" and NOT "the NULL-owner rows".
        assert visible_reports(db, actor_id=None, is_admin=False) == []


def test_load_visible_returns_none_for_another_users_report(app, hooks, client, session_factory):
    rid = uuid.UUID(file_report(client, title="alice's bug"))
    bob = FakeUser(username="bob")
    with session_factory() as db:
        assert load_visible(db, rid, actor_id=bob.id, is_admin=False) is None
        assert load_visible(db, rid, actor_id=None, is_admin=False) is None
        assert load_visible(db, rid, actor_id=hooks["actor"].id, is_admin=False) is not None
        assert load_visible(db, rid, actor_id=bob.id, is_admin=True) is not None
        # A missing row is the SAME answer as a forbidden one — no oracle at the predicate either.
        assert load_visible(db, uuid.uuid7(), actor_id=bob.id, is_admin=True) is None


# ── browser surface ──────────────────────────────────────────────────────────────


@pytest.fixture
def alices_report(client, hooks):
    """A real report owned by alice, with the session then switched to a *different* non-admin user."""
    rid = file_report(client, title="alice private bug", body="secret repro steps")
    hooks["actor"] = FakeUser(username="bob")
    return rid


def test_another_users_report_is_absent_from_the_list(client, alices_report):
    page = client.get("/bugreport/").get_data(as_text=True)
    assert "alice private bug" not in page
    assert "secret repro steps" not in page


@pytest.mark.parametrize("verb", ["update", "delete", "respond"])
def test_another_users_report_is_404_on_every_write_route(client, alices_report, verb):
    resp = client.post(f"/bugreport/{alices_report}/{verb}",
                       data={"title": "pwned", "body": "pwned", "status": "deleted", "note": "pwned"})
    assert resp.status_code == 404, f"{verb} leaked: {resp.status_code}"
    row = load(client, alices_report)
    assert row is not None, f"{verb} deleted another user's report"
    assert row.title == "alice private bug"
    assert row.body == "secret repro steps"
    assert row.status is ReportStatus.open
    assert row.admin_note is None


def test_a_forged_uuid_is_the_same_404(client, alices_report):
    """The refusal must not be distinguishable from a nonexistent id."""
    forged = client.post(f"/bugreport/{uuid.uuid7()}/update", data={"title": "x", "body": "y"})
    real = client.post(f"/bugreport/{alices_report}/update", data={"title": "x", "body": "y"})
    assert forged.status_code == real.status_code == 404


def test_an_admin_reaches_every_report(client, hooks, alices_report):
    hooks["actor"] = FakeUser(username="root", role="admin")
    page = client.get("/bugreport/").get_data(as_text=True)
    assert "alice private bug" in page
    resp = client.post(f"/bugreport/{alices_report}/respond",
                       data={"status": "acknowledged", "note": "seen, thanks"})
    assert resp.status_code == 302
    row = loaded(client, alices_report)
    assert row.status is ReportStatus.acknowledged and row.admin_note == "seen, thanks"


def test_a_non_admin_cannot_respond_to_their_OWN_report(client, hooks):
    """Visible != writable. The owner can reach the row (so it is 403, not 404) but the admin verb is
    still refused — otherwise any user could self-acknowledge and forge the feedback line."""
    rid = file_report(client, title="mine")
    resp = client.post(f"/bugreport/{rid}/respond", data={"status": "resolved", "note": "i fixed it"})
    assert resp.status_code == 403
    row = loaded(client, rid)
    assert row.status is ReportStatus.open and row.admin_note is None


def test_an_anonymous_session_sees_nothing_and_writes_nothing(client, hooks, alices_report):
    """A mounted host whose ``current_actor`` hook returns None is NOT standalone — nobody is logged in.
    The distinction is the fail-closed line in deps.py."""
    hooks["actor"] = None
    page = client.get("/bugreport/").get_data(as_text=True)
    assert "alice private bug" not in page
    assert client.post("/bugreport/", data={"title": "anon", "body": ""}).status_code == 403
    assert client.post(f"/bugreport/{alices_report}/update",
                       data={"title": "x", "body": ""}).status_code == 404
    assert loaded(client, alices_report).title == "alice private bug"


def test_a_non_uuid_actor_id_degrades_to_anonymous(client, hooks, alices_report):
    """A host that hands back an int id (a legacy/v1 host) must not accidentally match rows. lotek v2
    keys User on UUIDv7; anything else is treated as no identity, which is the closed direction."""
    hooks["actor"] = FakeUser(username="legacy", ident=7)
    assert "alice private bug" not in client.get("/bugreport/").get_data(as_text=True)
    assert client.post("/bugreport/", data={"title": "legacy", "body": ""}).status_code == 403


# ── machine (PAT) surface — the same rule, the other front door ──────────────────


@pytest.fixture
def alices_pat_report(pat_client, hooks):
    """A report filed by PAT-user alice, with the token then switched to a different non-admin token."""
    alice = StubActor(username="alice")
    hooks["pat_actor"] = alice
    resp = pat_client.post("/bugreport/machine/reports",
                           json={"title": "alice's agent bug", "body": "secret repro"})
    assert resp.status_code == 201, resp.data
    hooks["pat_actor"] = StubActor(username="bob")
    return resp.get_json()["report"]["id"]


def test_machine_list_is_scoped_to_the_token_user(pat_client, alices_pat_report):
    body = pat_client.get("/bugreport/machine/reports").get_json()
    assert body["reports"] == []


def test_machine_get_of_another_users_report_is_404(pat_client, alices_pat_report):
    resp = pat_client.get(f"/bugreport/machine/reports/{alices_pat_report}")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "not_found"


@pytest.mark.parametrize(
    "method,payload",
    [("patch", {"title": "pwned", "body": "pwned"}),
     ("patch", {"status": "deleted", "note": "pwned"}),
     ("delete", None)],
)
def test_machine_writes_on_another_users_report_are_404(pat_client, alices_pat_report, method, payload):
    call = getattr(pat_client, method)
    url = f"/bugreport/machine/reports/{alices_pat_report}"
    resp = call(url, json=payload) if payload is not None else call(url)
    assert resp.status_code == 404, resp.data
    row = load(pat_client, alices_pat_report)
    assert row is not None and row.title == "alice's agent bug"
    assert row.body == "secret repro" and row.status is ReportStatus.open


def test_machine_admin_token_reaches_every_report(pat_client, hooks, alices_pat_report):
    hooks["pat_actor"] = StubActor(username="root", role="admin")
    assert len(pat_client.get("/bugreport/machine/reports").get_json()["reports"]) == 1
    resp = pat_client.patch(f"/bugreport/machine/reports/{alices_pat_report}",
                            json={"status": "acknowledged", "note": "triaged"})
    assert resp.status_code == 200
    assert resp.get_json()["report"]["status"] == "acknowledged"


def test_machine_non_admin_cannot_set_status_on_their_own_report(pat_client, hooks):
    resp = pat_client.post("/bugreport/machine/reports", json={"title": "mine", "body": ""})
    rid = resp.get_json()["report"]["id"]
    denied = pat_client.patch(f"/bugreport/machine/reports/{rid}",
                              json={"status": "resolved", "note": "self-served"})
    assert denied.status_code == 403
    assert loaded(pat_client, rid).status is ReportStatus.open


def test_machine_admin_cannot_hard_delete_someone_elses_report(pat_client, hooks, alices_pat_report):
    """An admin's delete is a TOMBSTONE (PATCH status=deleted), not a row removal — otherwise the reporter
    can never learn what happened to it, which is the one thing #112 asks for."""
    hooks["pat_actor"] = StubActor(username="root", role="admin")
    assert pat_client.delete(f"/bugreport/machine/reports/{alices_pat_report}").status_code == 403
    assert load(pat_client, alices_pat_report) is not None


def test_an_admin_cannot_rewrite_someone_elses_report_text(pat_client, hooks, alices_pat_report):
    """An admin reaching a row is not an admin OWNING it. `load_visible` lets them through (they may
    read every report) and `update_own` still refuses — the README's "an admin responds to your report,
    they do not rewrite it" is enforced, not just documented. 403 rather than 404 here is correct: the
    admin already knows the row exists."""
    hooks["pat_actor"] = StubActor(username="root", role="admin")
    resp = pat_client.patch(f"/bugreport/machine/reports/{alices_pat_report}",
                            json={"title": "rewritten by an admin", "body": "not what alice wrote"})
    assert resp.status_code == 403
    row = loaded(pat_client, alices_pat_report)
    assert row.title == "alice's agent bug" and row.body == "secret repro"


def test_an_is_admin_METHOD_does_not_make_everyone_an_admin(client, hooks, alices_report):
    """`bool(<bound method>)` is True. If a host's User ever grows an `is_admin()` METHOD rather than a
    property, a `bool(getattr(actor, "is_admin", False))` read would silently promote EVERY logged-in
    user — and in this extension that predicate is the only thing widening a read past its owner."""
    class MethodUser:
        def __init__(self):
            self.id = uuid.uuid7()
            self.username = "bob"
            self.role = None

        def is_admin(self):  # a METHOD, not a property — truthy as an attribute
            return False

    hooks["actor"] = MethodUser()
    assert "alice private bug" not in client.get("/bugreport/").get_data(as_text=True)
    assert client.post(f"/bugreport/{alices_report}/respond",
                       data={"status": "deleted", "note": "pwned"}).status_code == 404
    assert loaded(client, alices_report).status is ReportStatus.open
