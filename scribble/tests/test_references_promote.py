"""Promote populates the typed ``references`` (#624) + CVE/CWE/OWASP metadata (#625) columns on
``EngagementFinding`` (map #616).

Driven through the REAL machine promote route against ``stub_host`` ``FakeFindingDTO``s — inputs are
simulated, the promote + merge logic runs for real (EDD: simulate inputs, never outputs).
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


def _template_with_refs(session_factory, refs: list[str]) -> uuid.UUID:
    """A HUMAN-authored library template carrying references. Inserted directly rather than via
    POST /templates because a machine-authored template is deliberately EXCLUDED from promote's automatic
    resolution (INV-EXT-02) — only a human-authored one is auto-adopted, which is the path #624's
    template+scan reference union runs on."""
    with session_factory() as db:
        t = fm.VulnerabilityTemplate(name=f"T-{uuid.uuid4().hex[:6]}", references=refs)
        db.add(t)
        db.flush()
        tid = t.id
        db.commit()
    return tid


def _map_source(client, *, source, template_id) -> None:
    r = client.post(f"{M}/vuln-map", json={"source": source, "template_id": template_id})
    assert r.status_code == 201, r.get_json()


def test_unmapped_promote_seeds_scan_refs_and_metadata(client, stub_host, session_factory, clean_vuln_map):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    dto = FakeFindingDTO(
        id=42, job_id="job-1", title="Reflected XSS", source="autorecon", severity="high",
        cve="CVE-2021-44228", references=["https://scan/x", "https://scan/x"],  # dup within the scan list
        facts={"host": "10.0.0.9", "cwe": "CWE-79"},
    )
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[dto])
    eid = _engagement(client, stub_host)

    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").get_json()["promoted"] == 1

    with session_factory() as db:
        row = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).one()
        # references: scan-sourced, deduped by url.
        assert [(r["url"], r["source"]) for r in row.references] == [("https://scan/x", "scan")]
        # #625 metadata seeded + derived.
        assert row.cve_ids == ["CVE-2021-44228"]
        assert row.cwe_ids == ["CWE-79"]
        assert row.owasp_categories == ["A03:2021"]   # CWE-79 -> A03 Injection, offline map
        assert row.threat_intel is None               # no exploiteer feed -> no snapshot


def test_template_match_promote_unions_template_and_scan_refs(
    client, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    tid = _template_with_refs(session_factory, ["https://tmpl/a", "https://shared/dup"])
    _map_source(client, source="enum4linux", template_id=tid)
    dto = FakeFindingDTO(
        id=99, title="SMB signing not required", source="enum4linux", severity="low",
        references=["https://shared/dup", "https://scan/b"], cve="CVE-2020-0001",
        facts={"cwe": "CWE-89"},
    )
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[dto])
    eid = _engagement(client, stub_host)

    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").get_json()["parents"] == 1

    with session_factory() as db:
        child = (
            db.query(fm.EngagementFinding)
            .filter(fm.EngagementFinding.engagement_id == eid, fm.EngagementFinding.parent_id.isnot(None))
            .one()
        )
        by_url = {r["url"]: r["source"] for r in child.references}
        # template refs (source=template) UNION scan refs (source=scan); the shared url is deduped, and
        # the template wins the collision (first group).
        assert by_url == {
            "https://tmpl/a": "template",
            "https://shared/dup": "template",
            "https://scan/b": "scan",
        }
        assert child.cve_ids == ["CVE-2020-0001"]
        assert child.cwe_ids == ["CWE-89"]
        assert child.owasp_categories == ["A03:2021"]   # CWE-89 SQLi -> A03
        # the PARENT (built from the template) carries the template refs only.
        parent = db.get(fm.EngagementFinding, child.parent_id)
        assert {r["url"] for r in parent.references} == {"https://tmpl/a", "https://shared/dup"}


def test_repromote_preserves_operator_reference_and_metadata_edits(
    client, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    stub_host.findings.add_job(
        "job-1", owner_id=7,
        dtos=[FakeFindingDTO(id=5, title="Open redirect", source="autorecon",
                             references=["https://scan/one"], cve="CVE-2020-0001")],
    )
    eid = _engagement(client, stub_host)
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").get_json()["promoted"] == 1

    # Operator suppresses the scan ref, adds an author ref, and edits cve_ids.
    with session_factory() as db:
        row = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).one()
        row.references = [
            {"label": "one", "url": "https://scan/one", "source": "scan", "suppressed": True},
            {"label": "vendor advisory", "url": "https://vendor/adv", "source": "author",
             "suppressed": False},
        ]
        row.cve_ids = ["CVE-2020-0001", "CVE-2020-9999"]
        db.commit()

    # Same finding re-promoted (source truth moved) -> skipped, NOT re-merged: fill-NULL-only (#617 Q5).
    body = client.post(f"{M}/engagements/{eid}/promote-job/job-1").get_json()
    assert body["promoted"] == 0 and body["skipped"] == 1

    with session_factory() as db:
        row = db.query(fm.EngagementFinding).filter_by(engagement_id=eid).one()
        assert [r["url"] for r in row.references] == ["https://scan/one", "https://vendor/adv"]
        assert row.references[0]["suppressed"] is True     # operator suppress preserved
        assert row.cve_ids == ["CVE-2020-0001", "CVE-2020-9999"]   # operator edit not clobbered
