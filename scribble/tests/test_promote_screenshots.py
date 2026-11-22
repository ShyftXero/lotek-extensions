"""Auto-attach a scan's host-page screenshot as evidence on a promoted web finding (ext#309).

httpx `-ss` writes a PNG per live page under `.../screenshot/<host>/..png`; the leader ingests the job
tree into the host object store as `kind=artifact` blobs keyed by the job. `promote.promote_job` matches
those to the web finding it promotes (by host, gated on the finding being a web URL) and bolts the shot
on as a `screenshot` Artifact — the evidence an operator used to upload by hand. These tests prove the
bytes actually LAND (not just that the code runs), that a re-promote does not duplicate, and that a
non-web finding sharing the host gets no (misleading) screenshot.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import scribble.models as fm
from scribble.artifacts_storage import artifact_bytes
from tests.conftest import FakeFindingDTO, StubActor

M = "/scribble/machine"
ACME = uuid.uuid7()
PNG = b"\x89PNG\r\n\x1a\n" + b"ShotBytes" + b"\x00" * 16


def _engagement(client, stub_host, name="E"):
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    resp = client.post(f"{M}/engagements", json={"name": name, "client_id": ACME})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    return uuid.UUID(body["id"]), body["core_engagement_id"]


def _put_screenshot(app, actor, *, job_id, path, data=PNG):
    """Seed a job-keyed `kind=artifact` blob at a httpx screenshot path, as ingest would."""
    objects = app.extensions["scribble"].extras["objects"]
    objects.put(actor, kind="artifact", stream=io.BytesIO(data), content_type="image/png",
                filename=path, job_id=job_id)


def _artifacts(session_factory, eid):
    with session_factory() as db:
        return db.query(fm.Artifact).filter_by(engagement_id=eid).all()


def test_promote_attaches_matching_screenshot_bytes_to_web_finding(
    app, client, stub_host, session_factory, clean_vuln_map
):
    actor = StubActor(id=7, username="opA", role="operator")
    stub_host.actor = actor
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1",
        owner_id=7,
        dtos=[FakeFindingDTO(
            id=1, title="Reflected XSS", source="dalfox", target_host="10.0.0.1",
            facts={"url": "http://10.0.0.1/search?q=x"},
        )],
        engagement_id=anchor,
    )
    _put_screenshot(app, actor, job_id="job-1",
                    path="modules/00_httpx-web/screenshot/10.0.0.1/shot.png")

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["promoted"] == 1

    arts = _artifacts(session_factory, eid)
    assert len(arts) == 1, "expected exactly one auto-attached screenshot"
    art = arts[0]
    assert art.kind == fm.ArtifactKind.screenshot
    assert art.finding_id is not None, "must attach to the finding, not the engagement"
    assert art.filename == "shot.png"
    assert art.sha256 == hashlib.sha256(PNG).hexdigest()
    # The bytes actually land: the Artifact's own evidence blob is readable and IS the shot.
    with app.app_context():
        assert artifact_bytes(art.storage_path) == PNG


def test_re_promote_does_not_duplicate_the_screenshot(
    app, client, stub_host, session_factory, clean_vuln_map
):
    actor = StubActor(id=7, username="opA", role="operator")
    stub_host.actor = actor
    eid, anchor = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1", owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="Reflected XSS", source="dalfox",
                             target_host="10.0.0.1", facts={"url": "http://10.0.0.1/"})],
        engagement_id=anchor,
    )
    _put_screenshot(app, actor, job_id="job-1",
                    path="modules/00_httpx-web/screenshot/10.0.0.1/shot.png")

    client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    client.post(f"{M}/engagements/{eid}/promote-job/job-1")  # re-promote (refresh, not duplicate)

    assert len(_artifacts(session_factory, eid)) == 1


def test_non_web_finding_on_a_web_host_gets_no_screenshot(
    app, client, stub_host, session_factory, clean_vuln_map
):
    actor = StubActor(id=7, username="opA", role="operator")
    stub_host.actor = actor
    eid, anchor = _engagement(client, stub_host)
    # A host finding with NO url (a port/service finding), same host as the screenshot: the web host-page
    # shot must not land as misleading evidence on it.
    stub_host.findings.add_job(
        "job-1", owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SMB signing not required", source="enum4linux",
                             target_host="10.0.0.1")],
        engagement_id=anchor,
    )
    _put_screenshot(app, actor, job_id="job-1",
                    path="modules/00_httpx-web/screenshot/10.0.0.1/shot.png")

    client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert _artifacts(session_factory, eid) == []
