"""The browser surface: file → admin responds → **the reporter sees what happened**.

That last leg is the half of #112 that is easy to leave out ("feedback about actions taken against a
user's reports should be given to the users"), so it is asserted on the rendered page, not on a row.
"""

from __future__ import annotations

import uuid

from conftest import FakeUser, file_report, load, loaded

from bugreport.models import MAX_BODY, MAX_TITLE, Report, ReportStatus
from bugreport.service import LIST_LIMIT, admin_act, visible_reports


def _page(client) -> str:
    resp = client.get("/bugreport/")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_file_then_read_back(client):
    file_report(client, title="scan wedges", body="steps: 1. run 2. wait forever")
    page = _page(client)
    assert "scan wedges" in page
    assert "steps: 1. run 2. wait forever" in page


def test_the_reporter_is_told_what_the_admin_did(client, hooks):
    """The feedback loop, end to end, on the reporter's own page."""
    rid = file_report(client, title="broken thing")
    reporter = hooks["actor"]

    hooks["actor"] = FakeUser(username="root", role="admin")
    assert client.post(f"/bugreport/{rid}/respond",
                       data={"status": "acknowledged", "note": "reproduced, tracking it"}).status_code == 302

    hooks["actor"] = reporter
    page = _page(client)
    assert "acknowledged" in page
    assert "reproduced, tracking it" in page
    assert 'data-testid="admin-feedback"' in page


def test_an_admin_delete_is_a_tombstone_the_reporter_can_still_see(client, hooks):
    """A hard delete cannot tell the reporter their report was deleted — which is the one thing #112
    asks for — so the admin's delete leaves the row and marks it."""
    rid = file_report(client, title="offensive report")
    reporter = hooks["actor"]

    hooks["actor"] = FakeUser(username="root", role="admin")
    assert client.post(f"/bugreport/{rid}/respond",
                       data={"status": "deleted", "note": "duplicate of an earlier one"}).status_code == 302

    hooks["actor"] = reporter
    page = _page(client)
    assert "deleted" in page and "duplicate of an earlier one" in page
    assert load(client, rid) is not None


def test_the_reporter_cannot_edit_an_admin_deleted_report_but_can_remove_it(client, hooks):
    rid = file_report(client, title="tombstoned")
    reporter = hooks["actor"]
    hooks["actor"] = FakeUser(username="root", role="admin")
    client.post(f"/bugreport/{rid}/respond", data={"status": "deleted", "note": "no"})

    hooks["actor"] = reporter
    assert client.post(f"/bugreport/{rid}/update",
                       data={"title": "rewritten", "body": ""}).status_code == 403
    assert loaded(client, rid).title == "tombstoned"
    # …but the reporter may still clear the tombstone once they have read it.
    assert client.post(f"/bugreport/{rid}/delete").status_code == 302
    assert load(client, rid) is None


def test_the_reporter_edits_and_deletes_their_own(client):
    rid = file_report(client, title="typo in title", body="old")
    assert client.post(f"/bugreport/{rid}/update",
                       data={"title": "fixed title", "body": "new"}).status_code == 302
    row = loaded(client, rid)
    assert (row.title, row.body) == ("fixed title", "new")
    assert client.post(f"/bugreport/{rid}/delete").status_code == 302
    assert load(client, rid) is None


def test_a_read_only_account_cannot_write(client, hooks):
    """`can_write` is the host's viewer gate. The UI hides the forms; the routes must refuse anyway."""
    rid = file_report(client, title="filed while writable")
    hooks["can_write"] = False
    page = _page(client)
    assert "read-only" in page
    assert client.post("/bugreport/", data={"title": "nope", "body": ""}).status_code == 403
    assert client.post(f"/bugreport/{rid}/update", data={"title": "nope", "body": ""}).status_code == 403
    assert client.post(f"/bugreport/{rid}/delete").status_code == 403
    assert loaded(client, rid).title == "filed while writable"


def test_empty_and_oversized_text_is_refused(client):
    assert client.post("/bugreport/", data={"title": "   ", "body": "x"}).status_code == 400
    assert client.post("/bugreport/",
                       data={"title": "a" * (MAX_TITLE + 1), "body": ""}).status_code == 400
    assert client.post("/bugreport/",
                       data={"title": "ok", "body": "b" * (MAX_BODY + 1)}).status_code == 400


def test_an_unknown_status_is_refused(client, hooks):
    rid = file_report(client, title="status fuzz")
    hooks["actor"] = FakeUser(username="root", role="admin")
    assert client.post(f"/bugreport/{rid}/respond",
                       data={"status": "closed-wont-fix", "note": ""}).status_code == 400
    assert loaded(client, rid).status is ReportStatus.open


def test_report_text_is_escaped_not_rendered(client):
    """Text-only means text-only: a report body is attacker-controlled and reaches an admin's browser."""
    file_report(client, title="xss <script>alert(1)</script>", body="<img src=x onerror=alert(2)>")
    page = _page(client)
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x onerror=alert(2)>" not in page
    assert "&lt;script&gt;" in page


def test_the_admin_response_note_is_escaped_too(client, hooks):
    rid = file_report(client, title="note escaping")
    reporter = hooks["actor"]
    hooks["actor"] = FakeUser(username="root", role="admin")
    client.post(f"/bugreport/{rid}/respond",
                data={"status": "resolved", "note": "<script>alert(3)</script>"})
    hooks["actor"] = reporter
    assert "<script>alert(3)</script>" not in _page(client)


def test_the_audit_seam_is_called_for_an_admin_action(client, hooks, audit_log):
    """INV-AUDIT-03: an admin acting on someone else's report appends to CORE's audit in the same
    transaction, namespaced `ext:<name>:<verb>` with before/after. Self-CRUD is not audited."""
    rid = file_report(client, title="audited")
    assert audit_log.events == []

    hooks["actor"] = FakeUser(username="root", role="admin")
    client.post(f"/bugreport/{rid}/respond", data={"status": "resolved", "note": "fixed in #99"})
    assert audit_log.actions() == ["ext:bugreport:admin_update"]
    event = audit_log.events[0]
    assert event["subject_type"] == "bugreport_report"
    assert str(event["subject_id"]) == rid
    assert event["before"]["status"] == "open"
    assert event["after"] == {"status": "resolved", "admin_note": "fixed in #99"}


def test_standalone_is_a_single_local_user_who_is_their_own_admin(standalone_app):
    """No host hooks at all -> the local user files and responds. This is the ONLY case where a report
    is written with a NULL reporter_id; deps.is_standalone() is what distinguishes it from anonymous."""
    client = standalone_app.test_client()
    resp = client.post("/bugreport/", data={"title": "local bug", "body": "no host here"})
    assert resp.status_code == 302
    page = client.get("/bugreport/").get_data(as_text=True)
    assert "local bug" in page
    # One local user owns everything, so there is no separate admin table to split it into.
    assert "All reports (admin)" not in page


# ── regressions found by the pre-PR adversarial review ───────────────────────────


def test_standalone_can_edit_and_delete_its_own_report(standalone_app):
    """W3: standalone writes `reporter_id = NULL` and has no actor id, so the ownership check refused the
    single local user their own rows — file-only, no U and no D."""
    client = standalone_app.test_client()
    rid = file_report(client, title="local", body="v1")
    assert client.post(f"/bugreport/{rid}/update",
                       data={"title": "local", "body": "v2"}).status_code == 302
    assert loaded(client, rid).body == "v2"
    assert client.post(f"/bugreport/{rid}/delete").status_code == 302
    assert load(client, rid) is None


def test_a_status_only_response_keeps_the_existing_note(client, hooks):
    """W1: the note IS the feedback #112 asks for. A second admin action that sends no note must not
    silently destroy the first one's."""
    rid = file_report(client, title="two-step triage")
    hooks["actor"] = FakeUser(username="root", role="admin")
    client.post(f"/bugreport/{rid}/respond", data={"status": "acknowledged", "note": "reproduced"})

    # A machine PATCH carrying only a status omits `note` entirely (the browser form always sends it).
    with client.application.extensions["bugreport"].session_factory() as db:
        admin_act(db, db.get(Report, uuid.UUID(rid)), is_admin=True, status="resolved", note=None)
    after = loaded(client, rid)
    assert after.status is ReportStatus.resolved
    assert after.admin_note == "reproduced"


def test_an_explicitly_empty_note_still_clears_it(client, hooks):
    rid = file_report(client, title="clearable")
    hooks["actor"] = FakeUser(username="root", role="admin")
    client.post(f"/bugreport/{rid}/respond", data={"status": "acknowledged", "note": "oops"})
    client.post(f"/bugreport/{rid}/respond", data={"status": "acknowledged", "note": ""})
    assert loaded(client, rid).admin_note is None


def test_every_list_surface_is_bounded(client, session_factory, hooks):
    """W2: filing is unrated-limited and each body is up to MAX_BODY, so an unbounded admin list is a
    memory-exhaustion lever any authenticated user can pull."""
    reporter_id = hooks["actor"].id
    with session_factory() as db:
        db.add_all(Report(reporter_id=reporter_id, title=f"r{i}", body="") for i in range(LIST_LIMIT + 25))
        db.commit()
    with session_factory() as db:
        assert len(visible_reports(db, actor_id=reporter_id, is_admin=False)) == LIST_LIMIT
        assert len(visible_reports(db, actor_id=reporter_id, is_admin=True)) == LIST_LIMIT
    # …and the rendered page shows no more than the cap either.
    assert _page(client).count('class="br-card') == LIST_LIMIT
