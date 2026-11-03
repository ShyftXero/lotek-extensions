"""#1343 final hop: promote links a dalfox finding's PoC screenshot to its ``BoardFinding``.

The dalfox XSS follow-on captures a screenshot proving the payload fired and the normalizer stamps its
job-output-relative path onto the finding (``evidence.poc_screenshot`` -> ``FindingDTO.facts``). This
proves promote materializes that fact into a scribble ``Artifact`` attached to the SPECIFIC finding,
referencing the runner-uploaded screenshot blob (``obj:<uuid>``) with NO byte copy and NO manual
correlation — the acceptance criterion the issue names.

Driven through the REAL machine promote route against a ``stub_host`` + the in-memory object surface
(EDD — simulate inputs, run the promote logic for real).
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import scribble.models as fm
from tests.conftest import FakeFindingDTO, StubActor

M = "/scribble/machine"
ACME = uuid.uuid7()
# A one-pixel PNG header is enough — the object surface stores opaque bytes; promote references, never reads.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _engagement(client, stub_host, name: str = "E"):
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    r = client.post(f"{M}/engagements", json={"name": name, "client_id": ACME})
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    return uuid.UUID(body["id"]), body["core_engagement_id"]


def _put_screenshot(app, *, engagement_id, filename):
    """Seed the runner-uploaded screenshot blob into the host object surface, as the artifact ingest
    would. Returns its ObjectRef (``.id``)."""
    objects = app.extensions["scribble"].extras["objects"]
    return objects.put(
        SimpleNamespace(id=1, username="op"), kind="screenshot",
        stream=io.BytesIO(PNG), content_type="image/png", filename=filename,
        engagement_id=engagement_id,
    )


def _xss_dto(*, fid, poc, job_id="job-1"):
    return FakeFindingDTO(
        id=fid, job_id=job_id, title="Reflected XSS in parameter 'q'", source="dalfox",
        severity="high", confidence="high", status="new", target_host="10.0.0.5",
        facts={"host": "10.0.0.5", "poc_screenshot": poc},
    )


def test_confirmed_xss_promotes_with_screenshot_attached(
    client, app, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    rel = "modules/05_xss_screenshot/screenshot/xss/0.png"
    ref = _put_screenshot(app, engagement_id=anchor, filename=rel)
    stub_host.findings.add_job(
        "job-1", owner_id=7, engagement_id=anchor, dtos=[_xss_dto(fid=7, poc=rel)]
    )

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200 and r.get_json()["promoted"] == 1

    with session_factory() as db:
        bf = db.query(fm.BoardFinding).filter_by(engagement_id=eid).one()
        art = db.query(fm.Artifact).filter_by(finding_id=bf.id).one()  # exactly one, on THIS finding
        assert art.kind is fm.ArtifactKind.screenshot
        assert art.storage_path == f"obj:{ref.id}"   # references the runner blob — no byte copy
        assert art.filename == "0.png"
        assert art.include_in_report is True


def test_xss_without_screenshot_attaches_nothing(
    client, app, stub_host, session_factory, clean_vuln_map
):
    """A dalfox hit the follow-on did NOT screenshot (no manifest entry) promotes clean, no artifact."""
    stub_host.actor = StubActor(id=7, username="opB", role="operator")
    eid, anchor = _engagement(client, stub_host)
    dto = FakeFindingDTO(
        id=8, job_id="job-1", title="Grep match", source="dalfox", severity="low",
        confidence="low", target_host="10.0.0.5", facts={"host": "10.0.0.5"},
    )
    stub_host.findings.add_job("job-1", owner_id=7, engagement_id=anchor, dtos=[dto])

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200 and r.get_json()["promoted"] == 1

    with session_factory() as db:
        bf = db.query(fm.BoardFinding).filter_by(engagement_id=eid).one()
        assert db.query(fm.Artifact).filter_by(finding_id=bf.id).count() == 0


def test_re_promote_does_not_duplicate_the_screenshot(
    client, app, stub_host, session_factory, clean_vuln_map
):
    """Idempotent: promoting the same job twice links the shot once, not twice."""
    stub_host.actor = StubActor(id=7, username="opC", role="operator")
    eid, anchor = _engagement(client, stub_host)
    rel = "modules/05_xss_screenshot/screenshot/xss/0.png"
    _put_screenshot(app, engagement_id=anchor, filename=rel)
    stub_host.findings.add_job(
        "job-1", owner_id=7, engagement_id=anchor, dtos=[_xss_dto(fid=7, poc=rel)]
    )

    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 200
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 200

    with session_factory() as db:
        bf = db.query(fm.BoardFinding).filter_by(engagement_id=eid).one()
        assert db.query(fm.Artifact).filter_by(finding_id=bf.id).count() == 1
