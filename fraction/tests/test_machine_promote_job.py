"""Machine API — bulk `POST /fraction/machine/engagements/<id>/promote-job/<job_id>`
(`fraction/promote.py::promote_job`, called from `fraction/api_pat.py`).

Ported from the deleted lotek `tests/test_api_v1_promote.py`. Proves: findings land + assignment is
recorded on the host (`host.mark_job_promoted`, RECORDED by `stub_host.promoted_calls` — the only
write this contract exposes back to the host), idempotent re-run, VulnMap-driven template selection,
and — again — the tenancy pass-through (missing/unauthorized job -> 404, nothing created).
"""

from __future__ import annotations

import fraction.models as fm
from tests.conftest import FakeFindingDTO, StubActor

M = "/fraction/machine"


def _engagement(client) -> int:
    return client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]


def test_promote_job_creates_findings_and_records_host_assignment(client, stub_host, session_factory):
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")]
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["promoted"] == 2 and body["skipped"] == 0

    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert {f.title for f in eng.findings} == {"SQLi", "XSS"}
        assert all(f.created_by == "opA" for f in eng.findings)

    # the ONE write this contract exposes back to the host is recorded, not silently dropped
    assert stub_host.promoted_calls == [("job-1", stub_host.actor, "fraction", eid)]


def test_promote_is_deduped_on_rerun(client, stub_host):
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi"), FakeFindingDTO(id=2, title="XSS")]
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client)
    client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    r2 = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r2.get_json() == {"engagement_id": eid, "promoted": 0, "skipped": 2, "parents": 0}


def test_promote_uses_vulnmap_template(client, stub_host, session_factory, clean_vuln_map):
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="nuclei hit", source="nuclei")]
    )
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client)
    tid = client.get(f"{M}/templates").get_json()["items"][0]["id"]
    client.post(f"{M}/vuln-map", json={"source": "nuclei", "template_id": tid})

    client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert eng.findings[0].template_id == tid  # promoted via from_template (VulnMap match)


def test_promote_respects_job_tenancy(client, stub_host):
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")])
    eid = _engagement(client)

    stub_host.actor = StubActor(id=8, username="opB", role="operator")  # doesn't own the job
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 404

    stub_host.actor = StubActor(id=7, username="opA", role="operator")  # owner
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 200


def test_promote_unknown_job_and_engagement_404(client, stub_host):
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="X")])
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = _engagement(client)
    assert client.post(f"{M}/engagements/{eid}/promote-job/nope").status_code == 404
    assert client.post(f"{M}/engagements/999999/promote-job/job-1").status_code == 404
