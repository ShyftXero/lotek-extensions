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
    return uuid.UUID(resp.get_json()["id"])


def test_human_promote_lands_findings_and_records_assignment(client, stub_host, session_factory):
    uid = _session_operator(stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=uid,
        dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")],
    )
    eid = _engagement(client, stub_host)

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "job-1"})
    assert r.status_code in (302, 303), r.data
    assert f"/scribble/engagements/{eid}" in r.headers["Location"]

    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert {f.title for f in eng.findings} == {"SQLi", "XSS"}
        assert all(f.created_by == "op" for f in eng.findings)
    # the ONE write the host contract exposes back is recorded with the SESSION actor, not dropped
    assert stub_host.promoted_calls == [("job-1", stub_host.current_user, "scribble", eid)]


def test_human_promote_unknown_job_is_a_noop_not_a_leak(client, stub_host, session_factory):
    _session_operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "does-not-exist"})
    assert r.status_code in (302, 303)  # redirect to the board — no crash, no 404 existence leak
    with session_factory() as db:
        assert list(db.get(fm.Engagement, eid).findings) == []
    assert stub_host.promoted_calls == []  # nothing recorded for a job that did not promote


def test_human_promote_empty_job_id_is_a_noop(client, stub_host, session_factory):
    _session_operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={})
    assert r.status_code in (302, 303)
    with session_factory() as db:
        assert list(db.get(fm.Engagement, eid).findings) == []
    assert stub_host.promoted_calls == []


def test_human_promote_denied_for_non_member(client, stub_host, session_factory):
    _session_operator(stub_host)
    eid = _engagement(client, stub_host)  # created under ACME, visible to the operator

    # Switch to an outsider who holds no grant: the blueprint gate must 404 BEFORE the view runs, even
    # though the outsider owns the job they're trying to pull in.
    stub_host.current_user = StubUser(id=91, username="outsider", role=_StubRole("operator"))
    stub_host.viewable_client_ids = set()
    stub_host.findings.add_job("job-9", owner_id=91, dtos=[FakeFindingDTO(id=1, title="X")])

    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "job-9"})
    assert r.status_code == 404  # the gate, not the view
    with session_factory() as db:
        assert list(db.get(fm.Engagement, eid).findings) == []


def test_human_promote_denied_for_viewer_without_write(client, stub_host, session_factory):
    """A user who can VIEW the engagement but has no write capability must NOT be able to promote — the
    UI hides the button (`scribble_can_write`), but that is a display flag, so the ROUTE enforces write
    too. (Removing the route's `host_can_write()` check turns this GREEN->RED: the promotion succeeds.)"""
    _session_operator(stub_host)
    stub_host.findings.add_job("job-1", owner_id=1, dtos=[FakeFindingDTO(id=1, title="SQLi")])
    eid = _engagement(client, stub_host)

    stub_host.can_write_value = False  # still a member (can view ACME), but read-only
    r = client.post(f"/scribble/engagements/{eid}/promote-job", data={"job_id": "job-1"})
    assert r.status_code == 403
    with session_factory() as db:
        assert list(db.get(fm.Engagement, eid).findings) == []
    assert stub_host.promoted_calls == []
