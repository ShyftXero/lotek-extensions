"""``source_facts`` snapshot + confidence/status mapping in promote (map #616 / #617).

``EngagementFinding`` is a lossless superset of the scan ``FindingDTO``: promote captures the WHOLE DTO
verbatim into ``source_facts`` — even on the template-match path, where ``from_template`` otherwise
discards the DTO's own title/severity/prose — and maps ``DTO.confidence``/``status`` onto the typed
columns that previously sat silently at their defaults (medium/new). Re-promote REFRESHES ``source_facts``
(source truth) but never clobbers a typed column an operator edited (#617 Q5, fill-NULL-only).

Driven through the REAL machine promote route (``api_pat.scribble_promote_job`` -> ``promote.promote_job``)
against ``stub_host`` ``FakeFindingDTO``s: inputs are simulated, the promote logic runs for real (EDD —
simulate inputs, never outputs).
"""

from __future__ import annotations

import uuid

import scribble.models as fm
from tests.conftest import FakeFindingDTO, StubActor

M = "/scribble/machine"
ACME = uuid.uuid7()


def _engagement(client, stub_host, name: str = "E") -> uuid.UUID:
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    r = client.post(f"{M}/engagements", json={"name": name, "client_id": ACME})
    assert r.status_code == 201, r.get_json()
    return uuid.UUID(r.get_json()["id"])


def _first_template_id(client) -> uuid.UUID:
    items = client.get(f"{M}/templates").get_json()["items"]
    assert items, "expected the seeded scribble library to have >=1 template"
    return uuid.UUID(items[0]["id"])


def _map_source(client, *, source, template_id) -> None:
    r = client.post(f"{M}/vuln-map", json={"source": source, "template_id": template_id})
    assert r.status_code == 201, r.get_json()


def test_unmapped_promote_captures_full_source_facts_and_maps_confidence_status(
    client, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    dto = FakeFindingDTO(
        id=42, job_id="job-1", title="Weak TLS", source="autorecon", severity="high",
        confidence="high", status="triaged", cve="CVE-2020-0001",
        references=["https://ex/1"], target_host="10.0.0.9", facts={"host": "10.0.0.9"},
        description="Server offers TLS 1.0.",
    )
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[dto])
    eid = _engagement(client, stub_host)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200 and r.get_json()["promoted"] == 1

    with session_factory() as db:
        row = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).one()
        # confidence/status mapped from the DTO — previously these defaulted silently to medium/new.
        assert row.confidence is fm.Confidence.high
        assert row.status is fm.FindingStatus.triaged
        # source_facts holds the whole DTO verbatim, INCLUDING fields with no typed column (cve, refs).
        sf = row.source_facts
        assert sf["id"] == 42
        assert sf["title"] == "Weak TLS"
        assert sf["cve"] == "CVE-2020-0001"
        assert sf["references"] == ["https://ex/1"]
        assert sf["confidence"] == "high" and sf["status"] == "triaged"
        assert sf["facts"] == {"host": "10.0.0.9"}


def test_template_match_promote_still_snapshots_the_source_dto(
    client, stub_host, session_factory, clean_vuln_map
):
    # On the template-match path from_template builds the row from the LIBRARY template and discards the
    # DTO's own title/severity/prose — source_facts is what makes that path lossless.
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    tid = _first_template_id(client)
    _map_source(client, source="enum4linux", template_id=tid)
    dto = FakeFindingDTO(
        id=99, title="SMB signing not required", source="enum4linux", severity="low",
        confidence="low", status="fixed", target_host="10.0.0.1", cve="CVE-1999-9999",
    )
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[dto])
    eid = _engagement(client, stub_host)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200 and r.get_json()["parents"] == 1

    with session_factory() as db:
        child = (
            db.query(fm.EngagementFinding)
            .filter(fm.EngagementFinding.engagement_id == eid, fm.EngagementFinding.parent_id.isnot(None))
            .one()
        )
        # confidence/status come from the DTO even though the visible fields came from the template.
        assert child.confidence is fm.Confidence.low
        assert child.status is fm.FindingStatus.fixed
        # the source DTO is preserved verbatim, including the title the template overrode.
        assert child.source_facts["id"] == 99
        assert child.source_facts["title"] == "SMB signing not required"
        assert child.source_facts["cve"] == "CVE-1999-9999"


def test_repromote_refreshes_source_facts_without_clobbering_edits(
    client, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    stub_host.findings.add_job(
        "job-1", owner_id=7,
        dtos=[FakeFindingDTO(id=5, title="Open redirect", source="autorecon", status="new")],
    )
    eid = _engagement(client, stub_host)
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").get_json()["promoted"] == 1

    # The operator edits the finding (title + status), THEN the upstream scan value moves.
    with session_factory() as db:
        row = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).one()
        row.title = "Open redirect (verified)"
        row.status = fm.FindingStatus.triaged
        db.commit()

    stub_host.findings.add_job(
        "job-1", owner_id=7,
        dtos=[FakeFindingDTO(id=5, title="Open redirect", source="autorecon", status="fixed")],
    )
    body = client.post(f"{M}/engagements/{eid}/promote-job/job-1").get_json()
    assert body["promoted"] == 0 and body["skipped"] == 1

    with session_factory() as db:
        row = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).one()
        assert row.title == "Open redirect (verified)"   # operator edit preserved
        assert row.status is fm.FindingStatus.triaged     # NOT stomped to the upstream "fixed"
        assert row.source_facts["status"] == "fixed"      # snapshot refreshed to source truth
