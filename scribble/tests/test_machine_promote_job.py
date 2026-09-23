"""Machine API — bulk `POST /scribble/machine/engagements/<id>/promote-job/<job_id>`
(`scribble/promote.py::promote_job`, called from `scribble/api_pat.py`).

Ported from the deleted lotek `tests/test_api_v1_promote.py`. Proves: findings land + assignment is
recorded on the host (`host.mark_job_promoted`, RECORDED by `stub_host.promoted_calls` — the only
write this contract exposes back to the host), idempotent re-run, VulnMap-driven template selection,
and — again — the tenancy pass-through (missing/unauthorized job -> 404, nothing created).
"""

from __future__ import annotations

import uuid

import scribble.models as fm
from tests.conftest import FakeFindingDTO, StubActor

_MISSING_ID = uuid.uuid7()  # a well-formed id that is not in the table

M = "/scribble/machine"


# Scribble's own client PK is UUIDv7 since lotek#335. Where a test seeds `scribble_clients` and
# ALSO grants on the same id via the stub host, both halves must move together.
ACME = uuid.uuid7()  # the client every machine-created engagement in this file belongs to


def _engagement(client, stub_host, name: str = "E") -> int:
    """Create the engagement under test — under a client THIS TOKEN can see.

    A machine engagement must now name a client the caller holds a grant under
    (`api_pat.scribble_create_engagement`, 2026-08-12). The client-less engagement these tests used to
    create is refused when mounted, because the host answers `can_view_client(None, actor) -> False`:
    it was readable and writable by nobody, including the tool that made it. The grant is set here
    rather than per test because it is a property of the fixture host, not of what any test asserts.
    """
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    resp = client.post(f"{M}/engagements", json={"name": name, "client_id": ACME})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    # Return the board's core-engagement anchor alongside its id: a promoted job must carry the SAME
    # engagement_id or promote_job's tenancy guard (lotek#845) refuses it 409.
    return uuid.UUID(body["id"]), body["core_engagement_id"]


def test_promote_job_creates_findings_and_records_host_assignment(client, stub_host, session_factory):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")],
        engagement_id=anchor,
    )

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["promoted"] == 2 and body["skipped"] == 0

    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        assert {f.title for f in eng.findings} == {"SQLi", "XSS"}
        assert all(f.created_by == "opA" for f in eng.findings)

    # the ONE write this contract exposes back to the host is recorded, not silently dropped
    assert stub_host.promoted_calls == [("job-1", stub_host.actor, "scribble", eid)]


def test_promote_is_deduped_on_rerun(client, stub_host):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")],
        engagement_id=anchor,
    )
    client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    r2 = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r2.get_json() == {
        "engagement_id": str(eid),  # ids serialise to strings on the wire
        "promoted": 0,
        "skipped": 2,
        "parents": 0,
    }


def test_promote_uses_vulnmap_template(client, stub_host, session_factory, clean_vuln_map):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="nuclei hit", source="nuclei")],
        engagement_id=anchor,
    )
    tid = uuid.UUID(client.get(f"{M}/templates").get_json()["items"][0]["id"])
    client.post(f"{M}/vuln-map", json={"source": "nuclei", "template_id": tid})

    client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        assert eng.findings[0].template_id == tid  # promoted via from_template (VulnMap match)


def test_promote_respects_job_tenancy(client, stub_host):
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], engagement_id=anchor
    )

    stub_host.actor = StubActor(id=8, username="opB", role="operator")  # doesn't own the job
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 404

    stub_host.actor = StubActor(id=7, username="opA", role="operator")  # owner
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 200


def test_promote_unknown_job_and_engagement_404(client, stub_host):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="X")], engagement_id=anchor
    )
    assert client.post(f"{M}/engagements/{eid}/promote-job/nope").status_code == 404
    assert client.post(f"{M}/engagements/{_MISSING_ID}/promote-job/job-1").status_code == 404


# ── the cross-engagement anchor guard (lotek#845) ───────────────────────────────────────────────────


def test_promote_refuses_cross_engagement_job(client, stub_host, session_factory):
    """A job whose core engagement is NOT this board's anchor is refused (lotek#845): 409 `cross_engagement`
    naming BOTH ids and how to fix it, nothing poured, and the host-side promote mark never fires.

    Non-vacuous: remove `assert_promote_anchor` from `promote_job` and this lands 2 findings + a 200, so
    the assertions below go red."""
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    other_engagement = uuid.uuid7()  # a DIFFERENT core engagement than the board's anchor
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], engagement_id=other_engagement
    )

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "cross_engagement"
    # both ids the operator needs, plus the actionable verb
    assert anchor in body["detail"] and str(other_engagement) in body["detail"]
    assert "Reassign" in body["detail"]

    assert stub_host.promoted_calls == []  # refused before the host-side mark_job_promoted
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []  # zero findings landed


def test_promote_refuses_job_with_no_engagement(client, stub_host, session_factory):
    """Fail-closed: a job with NO core engagement (`engagement_id` None — a shape prod never makes) cannot
    be shown to belong to this board, so promote refuses it 409 rather than silently pouring it in.

    Non-vacuous: the guard's `job is None` fail-closed clause is what makes this 409; without it the None
    job id would fall through and this would land a finding + 200."""
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, _anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], engagement_id=None
    )

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 409
    assert r.get_json()["error"] == "cross_engagement"
    assert stub_host.promoted_calls == []
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []
