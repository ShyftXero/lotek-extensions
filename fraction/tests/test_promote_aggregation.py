"""Parent aggregation in `fraction/promote.py::promote_job` — findings that resolve to the SAME
`FractionVulnMap` template are grouped under ONE parent `EngagementFinding`, each scan finding
becoming a host-attributed CHILD (`parent_id` set). An unmapped finding stays flat/ungrouped.

Ported from the deleted lotek `tests/test_promote_aggregation.py`, rewired onto the machine route +
`stub_host`'s `FakeFindingDTO`s (the internal-host-attribution case sets `target_host` directly on the
DTO, since deriving it from a raw evidence record / dedupe_key tail is `host_contract.py`'s job —
already proven in the lotek repo's own `tests/test_host_findings_contract.py` — this file proves only
that PROMOTE carries `dto.target_host` through to the child row).
"""

from __future__ import annotations

import fraction.models as fm
from tests.conftest import FakeFindingDTO, StubActor

M = "/fraction/machine"


def _engagement(client) -> int:
    return client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]


def _first_template_id(client) -> int:
    items = client.get(f"{M}/templates").get_json()["items"]
    assert items, "expected the seeded fraction library to have >=1 template"
    return items[0]["id"]


def _map_source(client, *, source, template_id) -> None:
    r = client.post(f"{M}/vuln-map", json={"source": source, "template_id": template_id})
    assert r.status_code == 201, r.get_json()


def test_promote_groups_same_template_into_one_parent_with_host_attributed_children(
    client, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    tid = _first_template_id(client)
    _map_source(client, source="enum4linux", template_id=tid)

    stub_host.findings.add_job(
        "job-1",
        owner_id=7,
        dtos=[
            FakeFindingDTO(
                id=1, title="SMB signing not required", source="enum4linux", target_host="10.0.0.1"
            ),
            FakeFindingDTO(
                id=2, title="SMB signing not required", source="enum4linux", target_host="10.0.0.2"
            ),
        ],
    )
    eid = _engagement(client)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["promoted"] == 2 and body["skipped"] == 0 and body["parents"] == 1

    with session_factory() as db:
        rows = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).all()
        parents = [row for row in rows if row.parent_id is None]
        children = [row for row in rows if row.parent_id is not None]
        assert len(parents) == 1
        assert len(children) == 2
        parent = parents[0]
        assert parent.template_id == tid
        assert parent.source_finding_id is None
        assert {c.parent_id for c in children} == {parent.id}
        assert {c.source_finding_id for c in children} == {1, 2}
        assert {c.target_host for c in children} == {"10.0.0.1", "10.0.0.2"}


def test_promote_rerun_does_not_duplicate_parent_or_children(
    client, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    tid = _first_template_id(client)
    _map_source(client, source="enum4linux", template_id=tid)
    stub_host.findings.add_job(
        "job-1",
        owner_id=7,
        dtos=[
            FakeFindingDTO(
                id=1, title="SMB signing not required", source="enum4linux", target_host="10.0.0.1"
            ),
            FakeFindingDTO(
                id=2, title="SMB signing not required", source="enum4linux", target_host="10.0.0.2"
            ),
        ],
    )
    eid = _engagement(client)

    r1 = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r1.get_json()["parents"] == 1

    r2 = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    body2 = r2.get_json()
    assert body2["promoted"] == 0 and body2["skipped"] == 2 and body2["parents"] == 0

    with session_factory() as db:
        rows = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).all()
        assert len([row for row in rows if row.parent_id is None]) == 1
        assert len([row for row in rows if row.parent_id is not None]) == 2


def test_promote_different_templates_get_separate_parents(
    client, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    items = client.get(f"{M}/templates").get_json()["items"]
    assert len(items) >= 2
    tid_a, tid_b = items[0]["id"], items[1]["id"]
    _map_source(client, source="enum4linux", template_id=tid_a)
    _map_source(client, source="dalfox", template_id=tid_b)

    stub_host.findings.add_job(
        "job-1",
        owner_id=7,
        dtos=[
            FakeFindingDTO(
                id=1, title="SMB signing not required", source="enum4linux", target_host="10.0.0.1"
            ),
            FakeFindingDTO(
                id=2, title="Reflected XSS in parameter 'q'", source="dalfox", target_host="10.0.0.1"
            ),
        ],
    )
    eid = _engagement(client)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    body = r.get_json()
    assert body["promoted"] == 2 and body["parents"] == 2

    with session_factory() as db:
        rows = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).all()
        parents = [row for row in rows if row.parent_id is None]
        assert {p.template_id for p in parents} == {tid_a, tid_b}
        assert len(parents) == 2


def test_promote_unmapped_findings_stay_flat_ungrouped(client, stub_host, session_factory, clean_vuln_map):
    """A finding that resolves to no template is bridged verbatim and stays flat (parent_id None, no
    separate parent row created)."""
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    stub_host.findings.add_job(
        "job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="Untitled scan hit", source="autorecon")]
    )
    eid = _engagement(client)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    body = r.get_json()
    assert body["promoted"] == 1 and body["parents"] == 0

    with session_factory() as db:
        rows = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).all()
        assert len(rows) == 1
        assert rows[0].parent_id is None
        assert rows[0].template_id is None


def test_promote_still_respects_job_tenancy_with_aggregation(
    client, stub_host, session_factory, clean_vuln_map
):
    """Existing authz boundary is unchanged by aggregation: a caller who can't view the job gets 404,
    and no parent/child rows are created for them."""
    tid = _first_template_id(client)
    _map_source(client, source="enum4linux", template_id=tid)
    stub_host.findings.add_job(
        "job-1",
        owner_id=7,
        dtos=[
            FakeFindingDTO(
                id=1, title="SMB signing not required", source="enum4linux", target_host="10.0.0.1"
            )
        ],
    )
    eid = _engagement(client)

    stub_host.actor = StubActor(id=8, username="opB", role="operator")
    r_b = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r_b.status_code == 404

    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert eng.findings == []  # opB's failed attempt created nothing

    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    r_a = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r_a.status_code == 200
    assert r_a.get_json()["parents"] == 1


def test_promote_attributes_internal_host_without_global_asset(
    client, stub_host, session_factory, clean_vuln_map
):
    """The child is still host-attributed for an INTERNAL engagement (no lotek `Asset` row) -- the
    host contract already recovered the host into `dto.target_host` (evidence record / dedupe_key
    tail fallback, proven in the lotek repo); this test proves PROMOTE carries that value through to
    the child row it creates, which is what makes the internal-AD marquee report show the DC per
    instance."""
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    tid = _first_template_id(client)
    _map_source(client, source="kerberoast", template_id=tid)

    stub_host.findings.add_job(
        "job-1",
        owner_id=7,
        dtos=[
            FakeFindingDTO(
                id=1, title="kerberoasting — 6 spns", source="kerberoast", target_host="192.168.57.10"
            ),
            FakeFindingDTO(
                id=2, title="kerberoasting — dc02", source="kerberoast", target_host="dc02.corp.local"
            ),
        ],
    )
    eid = _engagement(client)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200 and r.get_json()["parents"] == 1

    with session_factory() as db:
        children = (
            db.query(fm.EngagementFinding)
            .filter(fm.EngagementFinding.engagement_id == eid, fm.EngagementFinding.parent_id.isnot(None))
            .all()
        )
        assert len(children) == 2
        assert {c.target_host for c in children} == {"192.168.57.10", "dc02.corp.local"}
