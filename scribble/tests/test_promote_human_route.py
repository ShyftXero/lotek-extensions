"""Human (cookie/session) promote route: ``POST /scribble/engagements/<id>/promote-job``.

The browser twin of the machine promote route (`test_machine_promote_job.py`). Before this route existed,
an operator could promote a scan job's findings onto a report board ONLY over a PAT — there was no button
in the UI, so a person driving lotek in a browser could not turn a finished scan into a report at all
(found by driving the app as a fallible human, BusyBody 2026-08-27). These prove the human route lands the
findings, records the one host-side assignment, and shares the SAME tenancy contract as the machine twin:
the blueprint gate 404s a non-member, and an unknown/unauthorized job is an indistinguishable no-op.

`job_id` is a FORM field (not a URL segment) because there is no host hook to LIST an engagement's
promotable jobs yet — the operator supplies the id. That choice also keeps the route inside the generic
tenancy-gate loops (`test_scribble_tenancy_gate.py`), since the URL carries only the recognized
`engagement_id`.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

import scribble.models as fm
from tests.conftest import FakeFindingDTO, StubActor, StubUser, _StubRole

ACME = uuid.uuid7()  # the client every engagement here belongs to (see test_machine_promote_job.py)


def _session_operator(stub_host, uid: int = 1):
    """Drive as ONE operator across both surfaces: the session identity (`current_actor`) that the human
    promote route reads, and the PAT identity the machine helper below uses only to CREATE the fixture
    engagement."""
    stub_host.current_user = StubUser(id=uid, username="op", role=_StubRole("operator"))
    stub_host.actor = StubActor(id=uid, username="op", role="operator")
    return uid


def _engagement(client, stub_host, name: str = "E"):
    """Create the fixture engagement under a client the actor can see (machine route; the object is the
    same one the human route then promotes into)."""
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    resp = client.post("/scribble/machine/engagements", json={"name": name, "client_id": ACME})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    # (id, core-engagement anchor) — a promoted job must carry the SAME engagement_id or promote_job's
    # tenancy guard (lotek#845) refuses it 409.
    return uuid.UUID(body["id"]), body["core_engagement_id"]


def test_human_promote_lands_findings_and_records_assignment(client, stub_host, session_factory):
    uid = _session_operator(stub_host)
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=uid,
        dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")],
        engagement_id=anchor,
    )

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "job-1"})
    assert r.status_code in (302, 303), r.data
    assert f"/scribble/engagements/{eid}" in r.headers["Location"]

    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        assert {f.title for f in eng.findings} == {"SQLi", "XSS"}
        assert all(f.created_by == "op" for f in eng.findings)
    # the ONE write the host contract exposes back is recorded with the SESSION actor, not dropped
    assert stub_host.promoted_calls == [("job-1", stub_host.current_user, "scribble", eid)]


def test_human_promote_unknown_job_is_a_noop_not_a_leak(client, stub_host, session_factory):
    _session_operator(stub_host)
    eid, _anchor = _engagement(client, stub_host)

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "does-not-exist"})
    assert r.status_code in (302, 303)  # redirect to the board — no crash, no 404 existence leak
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []
    assert stub_host.promoted_calls == []  # nothing recorded for a job that did not promote


def test_human_promote_empty_job_id_is_a_noop(client, stub_host, session_factory):
    _session_operator(stub_host)
    eid, _anchor = _engagement(client, stub_host)

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={})
    assert r.status_code in (302, 303)
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []
    assert stub_host.promoted_calls == []


def test_human_promote_denied_for_non_member(client, stub_host, session_factory):
    _session_operator(stub_host)
    eid, _anchor = _engagement(client, stub_host)  # created under ACME, visible to the operator

    # Switch to an outsider who holds no grant: the blueprint gate must 404 BEFORE the view runs, even
    # though the outsider owns the job they're trying to pull in.
    stub_host.current_user = StubUser(id=91, username="outsider", role=_StubRole("operator"))
    stub_host.viewable_client_ids = set()
    stub_host.findings.add_job("job-9", owner_id=91, dtos=[FakeFindingDTO(id=1, title="X")])

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "job-9"})
    assert r.status_code == 404  # the gate, not the view
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []


def test_human_promote_denied_for_viewer_without_write(client, stub_host, session_factory):
    """A user who can VIEW the engagement but has no write capability must NOT be able to promote — the
    UI hides the button (`scribble_can_write`), but that is a display flag, so the ROUTE enforces write
    too. (Removing the route's `host_can_write()` check turns this GREEN->RED: the promotion succeeds.)"""
    _session_operator(stub_host)
    stub_host.findings.add_job("job-1", owner_id=1, dtos=[FakeFindingDTO(id=1, title="SQLi")])
    eid, _anchor = _engagement(client, stub_host)

    stub_host.can_write_value = False  # still a member (can view ACME), but read-only
    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "job-1"})
    assert r.status_code == 403
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []
    assert stub_host.promoted_calls == []


def test_human_promote_refuses_cross_engagement(client, stub_host, session_factory):
    """The human twin enforces the #845 anchor too: a job whose core engagement is NOT this board's anchor
    aborts 409, lands nothing, and never records the host-side promote mark.

    Non-vacuous: without `assert_promote_anchor` in `promote_job`, the mismatched job lands its finding and
    the route 302-redirects — so both the 409 and the empty-board assertions go red."""
    uid = _session_operator(stub_host)
    eid, _anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        engagement_id=uuid.uuid7(),  # a DIFFERENT core engagement than the board's anchor
    )

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "job-1"})
    assert r.status_code == 409, r.data
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []
    assert stub_host.promoted_calls == []


# ── one-click add-job via the by-core resolver (#847) ────────────────────────────────────────────────
#
# A core job-page button POSTs to `/scribble/engagements/by-core/<core_id>/adopt-job/<job>` (CSRF-safe —
# a write must not be a GET). The POST resolves-or-CREATES the board for that core engagement (the shared
# `_board_for_core` seam import_board uses — a board is never an orphan) and adopts the job onto it via the
# shared `_adopt_job_onto_board` body adopt_job uses, so the #845 anchor and #632 refuse-on-conflict behave
# identically. The plain GET by-core resolver stays a pure #632 reverse-link (no write): board -> board,
# no board -> the Report Boards list (#231 retired the standalone create form).


def test_by_core_one_click_resolves_and_adopts_the_job(client, stub_host, session_factory):
    """The board EXISTS: POSTing the job to it by core id lands on the board AND pours the job's findings,
    recording the one host-side mark. Non-vacuous: drop the by-core `_adopt_job_onto_board` call and the
    findings/mark assertions go red (it would only redirect)."""
    uid = _session_operator(stub_host)
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=uid,
        dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")],
        engagement_id=anchor,
    )

    r = client.post(f"/scribble/engagements/by-core/{anchor}/adopt-job/job-1")
    assert r.status_code in (302, 303), r.data
    assert f"/scribble/engagements/{eid}" in r.headers["Location"]  # lands on the resolved board
    with session_factory() as db:
        assert {f.title for f in db.get(fm.ReportBoard, eid).findings} == {"SQLi", "XSS"}
    assert stub_host.promoted_calls == [("job-1", stub_host.current_user, "scribble", eid)]


def test_by_core_without_job_id_only_resolves(client, stub_host, session_factory):
    """Plain #632 resolution is unchanged: no ?job_id -> redirect to the board, nothing adopted."""
    _session_operator(stub_host)
    eid, anchor = _engagement(client, stub_host)

    r = client.get(f"/scribble/engagements/by-core/{anchor}")
    assert r.status_code in (302, 303), r.data
    assert f"/scribble/engagements/{eid}" in r.headers["Location"]
    assert stub_host.promoted_calls == []
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []


def test_by_core_get_absent_board_redirects_to_list(client, stub_host):
    """The GET resolver is a pure #632 reverse-link: no board for this core id -> the Report Boards list
    (#231 retired the standalone create form). Creating a board is the POST one-click's job, not the GET's."""
    _session_operator(stub_host)
    missing_core = uuid.uuid7()

    r = client.get(f"/scribble/engagements/by-core/{missing_core}")
    assert r.status_code in (302, 303), r.data
    assert r.headers["Location"].endswith("/scribble/engagements")


def test_by_core_one_click_refuses_cross_engagement(client, stub_host, session_factory):
    """The by-core one-click enforces the #845 anchor exactly like adopt_job: a job whose core engagement
    is NOT this board's anchor aborts 409, lands nothing, and never records the host-side mark."""
    uid = _session_operator(stub_host)
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        engagement_id=uuid.uuid7(),  # a DIFFERENT core engagement than the board's anchor
    )

    r = client.post(f"/scribble/engagements/by-core/{anchor}/adopt-job/job-1")
    assert r.status_code == 409, r.data
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []
    assert stub_host.promoted_calls == []  # anchor check is first, before the mark


def test_by_core_one_click_refuses_already_adopted_elsewhere(client, stub_host, session_factory):
    """Refuse-on-conflict (#632) via the one-click path: a job already promoted into a DIFFERENT
    engagement 409s, pours nothing here, and is NOT re-pointed off the engagement that owns it."""
    uid = _session_operator(stub_host)
    eid, anchor = _engagement(client, stub_host)
    # Anchor the job to THIS board so the 409 comes from the conflict, not the #845 mismatch.
    stub_host.findings.add_job(
        "job-1", owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")], engagement_id=anchor,
    )
    other_eid = uuid.uuid7()
    stub_host.add_promoted_job(other_eid, "job-1")  # already linked into a DIFFERENT engagement

    r = client.post(f"/scribble/engagements/by-core/{anchor}/adopt-job/job-1")
    assert r.status_code == 409, r.data
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []
    # Not re-pointed: still owned by `other`, nothing linked to this board.
    assert [j.id for j in stub_host.list_jobs(None, extension="scribble", ref_id=other_eid)] == ["job-1"]
    assert stub_host.list_jobs(None, extension="scribble", ref_id=eid) == []


def test_by_core_one_click_creates_board_when_absent_and_adopts(client, stub_host, session_factory):
    """No board yet for the core engagement: the POST one-click CREATES it (name + client DERIVED from the
    host summary — the same `_board_for_core` seam import uses) AND adopts the job onto it in the one
    request, so one click from a core job page works whether or not a board exists yet. Non-vacuous:
    without the create the route 404s (no board), and without the adopt the board is empty + no mark."""
    uid = _session_operator(stub_host)
    core = uuid.uuid7()
    stub_host.engagement_summaries_value = [
        {"id": core, "name": "E", "client_id": ACME, "client_name": "Acme"},
    ]
    stub_host.findings.add_job(
        "job-1", owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")], engagement_id=core,
    )

    r = client.post(f"/scribble/engagements/by-core/{core}/adopt-job/job-1")
    assert r.status_code in (302, 303), r.data
    with session_factory() as db:
        eng = db.scalars(select(fm.ReportBoard)).one()  # exactly one board, freshly created
        assert eng.core_engagement_id == core and eng.name == "E"
        assert f"/scribble/engagements/{eng.id}" in r.headers["Location"]
        assert {f.title for f in eng.findings} == {"SQLi"}
        assert stub_host.promoted_calls == [("job-1", stub_host.current_user, "scribble", eng.id)]


def test_by_core_one_click_cross_engagement_creates_no_orphan_board(client, stub_host, session_factory):
    """The create-then-409 rollback the whole one-click safety rests on: NO board exists yet AND the job is
    cross-engagement, so `_board_for_core` creates+flushes a board and `_adopt_job_onto_board` then 409s
    (the #845 anchor) BEFORE the commit — the fresh board must roll back with it, leaving ZERO boards.
    Pins the "commit is last" invariant: add an early `db.commit()` in `_board_for_core` and this goes red."""
    uid = _session_operator(stub_host)
    core = uuid.uuid7()
    stub_host.engagement_summaries_value = [
        {"id": core, "name": "E", "client_id": ACME, "client_name": "Acme"},
    ]
    stub_host.findings.add_job(
        "job-1", owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        engagement_id=uuid.uuid7(),  # a DIFFERENT core engagement than the board would anchor to
    )

    r = client.post(f"/scribble/engagements/by-core/{core}/adopt-job/job-1")
    assert r.status_code == 409, r.data
    with session_factory() as db:
        assert db.scalars(select(fm.ReportBoard)).all() == []  # the freshly-created board rolled back
    assert stub_host.promoted_calls == []  # anchor check is first, before the mark


def test_by_core_one_click_denied_for_non_operator(client, stub_host, session_factory):
    """The write gate: a caller who is not an operator on the core engagement gets an indistinguishable
    404 (`can_operate_on`), never a 403/redirect that would confirm the engagement exists. No board is
    created (even though a summary is visible), nothing adopted, no host mark."""
    uid = _session_operator(stub_host)
    core = uuid.uuid7()
    stub_host.operable_engagement_ids = set()  # operator on NOTHING -> can_operate_on() is False
    stub_host.engagement_summaries_value = [
        {"id": core, "name": "E", "client_id": ACME, "client_name": "Acme"},
    ]
    stub_host.findings.add_job(
        "job-1", owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")], engagement_id=core,
    )

    r = client.post(f"/scribble/engagements/by-core/{core}/adopt-job/job-1")
    assert r.status_code == 404, r.data
    with session_factory() as db:
        assert db.scalars(select(fm.ReportBoard)).all() == []
    assert stub_host.promoted_calls == []
