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
    return uuid.UUID(resp.get_json()["id"])


def test_promote_job_creates_findings_and_records_host_assignment(client, stub_host, session_factory):
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")]
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["promoted"] == 2 and body["skipped"] == 0

    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert {f.title for f in eng.findings} == {"SQLi", "XSS"}
        assert all(f.created_by == "opA" for f in eng.findings)

    # the ONE write this contract exposes back to the host is recorded, not silently dropped
    assert stub_host.promoted_calls == [("job-1", stub_host.actor, "scribble", eid)]


def test_promote_is_deduped_on_rerun(client, stub_host):
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")]
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)
    client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    r2 = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r2.get_json() == {
        "engagement_id": str(eid),  # ids serialise to strings on the wire
        "promoted": 0,
        "skipped": 2,
        "parents": 0,
    }


def test_promote_uses_vulnmap_template(client, stub_host, session_factory, clean_vuln_map):
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="nuclei hit", source="nuclei")]
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)
    tid = uuid.UUID(client.get(f"{M}/templates").get_json()["items"][0]["id"])
    client.post(f"{M}/vuln-map", json={"source": "nuclei", "template_id": tid})

    client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert eng.findings[0].template_id == tid  # promoted via from_template (VulnMap match)


def test_promote_respects_job_tenancy(client, stub_host):
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")])
    eid = _engagement(client, stub_host)

    stub_host.actor = StubActor(id=8, username="opB", role="operator")  # doesn't own the job
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 404

    stub_host.actor = StubActor(id=7, username="opA", role="operator")  # owner
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 200


def test_promote_unknown_job_and_engagement_404(client, stub_host):
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="X")])
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)
    assert client.post(f"{M}/engagements/{eid}/promote-job/nope").status_code == 404
    assert client.post(f"{M}/engagements/{_MISSING_ID}/promote-job/job-1").status_code == 404


# ── #656 scan-outcome honesty: refuse promoting an unassessed job into a client deliverable ──────────


def test_unassessed_job_is_refused_without_acknowledge(client, stub_host, session_factory):
    # A job the scan could not run (assessed=False) has zero findings for the WRONG reason. Promoting it
    # would render "No issues identified" — absence of evidence as evidence of absence. Refuse it.
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[], assessed=False, unassessed_modules=("bloodhound_python", "netexec_smb")
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "inconclusive_job"
    assert body["unassessed_modules"] == ["bloodhound_python", "netexec_smb"]

    with session_factory() as db:  # nothing promoted, nothing recorded on the host
        assert db.get(fm.Engagement, eid).findings == []
    assert stub_host.promoted_calls == []


def test_acknowledge_inconclusive_allows_promote_and_records_coverage(client, stub_host, session_factory):
    stub_host.findings.add_job(
        "job-1",
        owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=False,
        unassessed_modules=("netexec_smb",),
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)

    r = client.post(
        f"{M}/engagements/{eid}/promote-job/job-1", json={"acknowledge_inconclusive": True}
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["promoted"] == 1
    assert body["unassessed_modules"] == ["netexec_smb"]
    assert body["coverage_acknowledged"] is True

    with session_factory() as db:
        assert {f.title for f in db.get(fm.Engagement, eid).findings} == {"SQLi"}
    gaps = [c for c in stub_host.audit_calls if c[0] == "ext:scribble:promote_coverage_gap"]
    assert len(gaps) == 1
    after = gaps[0][1]["after"]
    assert after["acknowledged_inconclusive"] is True
    assert after["unassessed_modules"] == ["netexec_smb"]


def test_acknowledge_via_query_param_also_works(client, stub_host):
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], assessed=False
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)
    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1?acknowledge_inconclusive=true")
    assert r.status_code == 200


def test_legacy_unmeasured_job_is_not_refused(client, stub_host):
    # assessed=None means coverage was never measured (a job predating evidence_bytes). The gate must NOT
    # refuse it — the report must not assert what it cannot measure — and the response stays unchanged.
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], assessed=None)
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)
    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200
    assert "unassessed_modules" not in r.get_json()  # no gap to report → response unchanged


def test_assessed_job_with_partial_gaps_records_coverage_without_acknowledge(client, stub_host):
    # assessed=True (it ran and produced evidence) but some modules were still unassessed. Not refused,
    # but the coverage gap is recorded and surfaced so the deliverable can be honest about it.
    stub_host.findings.add_job(
        "job-1",
        owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=True,
        unassessed_modules=("azurehound",),
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client, stub_host)
    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200
    assert r.get_json()["unassessed_modules"] == ["azurehound"]
    assert r.get_json()["coverage_acknowledged"] is False
    assert any(c[0] == "ext:scribble:promote_coverage_gap" for c in stub_host.audit_calls)
