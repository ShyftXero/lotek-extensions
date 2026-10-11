"""Per-finding promote to Scribble (#1323): the promote-selected route + disposition skip + cross-job
observation dedupe, on the machine blueprint.

Reuses the ONE promote implementation (``promote.promote_job`` on a selected subset) — these prove the
seam-level contract: a selected subset promotes, FP/accepted_risk never does, a repeat scan's identical
observation collapses onto the existing row instead of a duplicate affected-host row, and the route
stamps each promoted core finding through the host ``mark_finding_promoted`` seam.
"""
from __future__ import annotations

import uuid

import scribble.models as fm
from scribble.promote import FindingNotInJob, select_job_findings
from tests.conftest import FakeFindingDTO, StubActor, StubUser, _StubRole

M = "/scribble/machine"
ACME = uuid.uuid7()


def _engagement(client, stub_host, name: str = "E"):
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    resp = client.post(f"{M}/engagements", json={"name": name, "client_id": ACME})
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    return uuid.UUID(body["id"]), body["core_engagement_id"]


def _first_template_id(client) -> uuid.UUID:
    items = client.get(f"{M}/templates").get_json()["items"]
    assert items
    return uuid.UUID(items[0]["id"])


def _map_source(client, *, source, template_id) -> None:
    r = client.post(f"{M}/vuln-map", json={"source": source, "template_id": template_id})
    assert r.status_code == 201, r.get_json()


# ── select_job_findings: the ids-belong-to-this-job guard ─────────────────────────────────────────


def test_select_job_findings_rejects_id_outside_job():
    dtos = [FakeFindingDTO(id=uuid.uuid7(), title="a"), FakeFindingDTO(id=uuid.uuid7(), title="b")]
    try:
        select_job_findings(dtos, [str(dtos[0].id), str(uuid.uuid7())])
    except FindingNotInJob:
        pass
    else:
        raise AssertionError("expected FindingNotInJob for an id not in the job")


def test_select_job_findings_returns_subset_in_job_order():
    a, b, c = (FakeFindingDTO(id=uuid.uuid7(), title=t) for t in ("a", "b", "c"))
    # request out of order + a duplicate -> result follows dtos order, de-duped
    out = select_job_findings([a, b, c], [str(c.id), str(a.id), str(a.id)])
    assert [d.title for d in out] == ["a", "c"]


# ── the route: selected subset only ───────────────────────────────────────────────────────────────


def test_promote_findings_promotes_only_the_selected(client, stub_host, session_factory, clean_vuln_map):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    f1, f2, f3 = (uuid.uuid7(), uuid.uuid7(), uuid.uuid7())
    stub_host.findings.add_job(
        "job-1", owner_id=7, engagement_id=anchor,
        dtos=[
            FakeFindingDTO(id=f1, title="finding one", target_host="10.0.0.1"),
            FakeFindingDTO(id=f2, title="finding two", target_host="10.0.0.2"),
            FakeFindingDTO(id=f3, title="finding three", target_host="10.0.0.3"),
        ],
    )
    r = client.post(f"{M}/engagements/{eid}/promote-findings/job-1",
                    json={"finding_ids": [str(f1), str(f3)]})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["promoted"] == 2 and body["stamped"] == 2
    with session_factory() as db:
        titles = {row.title for row in db.query(fm.BoardFinding).filter_by(engagement_id=eid).all()}
    assert titles == {"finding one", "finding three"}  # finding two was NOT selected
    # Each promoted core finding stamped via the host seam, with its board ref.
    stamped_ids = {fid for fid, _actor, ext, _ref in stub_host.finding_promotion_calls if ext == "scribble"}
    assert stamped_ids == {f1, f3}


def test_promote_findings_404_for_id_outside_job(client, stub_host, session_factory, clean_vuln_map):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    f1 = uuid.uuid7()
    stub_host.findings.add_job(
        "job-1", owner_id=7, engagement_id=anchor,
        dtos=[FakeFindingDTO(id=f1, title="finding one", target_host="10.0.0.1")],
    )
    r = client.post(f"{M}/engagements/{eid}/promote-findings/job-1",
                    json={"finding_ids": [str(f1), str(uuid.uuid7())]})
    assert r.status_code == 404, r.get_json()
    with session_factory() as db:
        assert db.query(fm.BoardFinding).filter_by(engagement_id=eid).count() == 0  # nothing poured


def test_promote_findings_bad_body_400(client, stub_host, clean_vuln_map):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, _anchor = _engagement(client, stub_host)
    r = client.post(f"{M}/engagements/{eid}/promote-findings/job-1", json={"finding_ids": "nope"})
    assert r.status_code == 400


# ── disposition skip: false_positive / accepted_risk never promote ────────────────────────────────


def test_promote_findings_skips_false_positive_and_accepted_risk(
    client, stub_host, session_factory, clean_vuln_map
):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid, anchor = _engagement(client, stub_host)
    real, fp, risk = (uuid.uuid7(), uuid.uuid7(), uuid.uuid7())
    stub_host.findings.add_job(
        "job-1", owner_id=7, engagement_id=anchor,
        dtos=[
            FakeFindingDTO(id=real, title="real", status="new", target_host="10.0.0.1"),
            FakeFindingDTO(id=fp, title="a false positive", status="false_positive", target_host="10.0.0.2"),
            FakeFindingDTO(id=risk, title="accepted", status="accepted_risk", target_host="10.0.0.3"),
        ],
    )
    r = client.post(f"{M}/engagements/{eid}/promote-findings/job-1",
                    json={"finding_ids": [str(real), str(fp), str(risk)]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["promoted"] == 1  # only the `new` finding
    with session_factory() as db:
        titles = {row.title for row in db.query(fm.BoardFinding).filter_by(engagement_id=eid).all()}
    assert titles == {"real"}
    stamped = {fid for fid, _a, ext, _r in stub_host.finding_promotion_calls if ext == "scribble"}
    assert stamped == {real}  # FP/accepted-risk are never stamped either


# ── cross-job observation dedupe: a repeat scan adds no new affected-host row ──────────────────────


def test_repeat_scan_same_host_dedupes_to_one_child(client, stub_host, session_factory, clean_vuln_map):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    tid = _first_template_id(client)
    _map_source(client, source="enum4linux", template_id=tid)
    eid, anchor = _engagement(client, stub_host)

    # Job 1: one finding, 10.0.0.1 -> 1 parent + 1 child.
    j1f = uuid.uuid7()
    stub_host.findings.add_job(
        "job-1", owner_id=7, engagement_id=anchor,
        dtos=[FakeFindingDTO(id=j1f, title="SMB signing not required",
                             source="enum4linux", target_host="10.0.0.1")],
    )
    r1 = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r1.status_code == 200 and r1.get_json()["parents"] == 1

    # Job 2 (a REPEAT scan): a DIFFERENT finding id, SAME template + SAME host.
    j2f = uuid.uuid7()
    stub_host.findings.add_job(
        "job-2", owner_id=7, engagement_id=anchor,
        dtos=[FakeFindingDTO(id=j2f, title="SMB signing not required",
                             source="enum4linux", target_host="10.0.0.1")],
    )
    r2 = client.post(f"{M}/engagements/{eid}/promote-job/job-2")
    assert r2.status_code == 200
    body2 = r2.get_json()
    # The second observation collapses onto the existing child — no new affected-host row.
    assert body2["promoted"] == 0 and body2["skipped"] == 1

    with session_factory() as db:
        rows = db.query(fm.BoardFinding).filter_by(engagement_id=eid).all()
        children = [row for row in rows if row.parent_id is not None]
        assert len(children) == 1  # ONE affected-host row for 10.0.0.1, not two


# ── browser (cookie) route: by-core promote-findings (Plague #298 r1 CONCERN 4) ───────────────────


def _session_operator(stub_host, uid: int = 7):
    stub_host.current_user = StubUser(id=uid, username="op", role=_StubRole("operator"))
    stub_host.actor = StubActor(id=uid, username="op", role="operator")
    return uid


def test_browser_promote_findings_promotes_only_selected(client, stub_host, session_factory, clean_vuln_map):
    uid = _session_operator(stub_host)
    eid, anchor = _engagement(client, stub_host)  # board's core_engagement_id == anchor
    f1, f2 = uuid.uuid7(), uuid.uuid7()
    stub_host.findings.add_job(
        "job-1", owner_id=uid, engagement_id=anchor,
        dtos=[FakeFindingDTO(id=f1, title="A", target_host="10.0.0.1"),
              FakeFindingDTO(id=f2, title="B", target_host="10.0.0.2")],
    )
    # by-core resolves the existing board (core_engagement_id == anchor); finding_ids is a form field.
    r = client.post(f"/scribble/engagements/by-core/{anchor}/promote-findings/job-1",
                    data={"finding_ids": [str(f1)]})
    assert r.status_code in (302, 303), r.data
    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        assert {f.title for f in eng.findings} == {"A"}
    stamped = {fid for fid, _a, ext, _r in stub_host.finding_promotion_calls if ext == "scribble"}
    assert stamped == {f1}  # stamped with the SESSION actor, only the selected finding


def test_browser_promote_findings_404_for_id_outside_job(client, stub_host, session_factory, clean_vuln_map):
    uid = _session_operator(stub_host)
    _eid, anchor = _engagement(client, stub_host)
    f1 = uuid.uuid7()
    stub_host.findings.add_job(
        "job-1", owner_id=uid, engagement_id=anchor,
        dtos=[FakeFindingDTO(id=f1, title="A", target_host="10.0.0.1")],
    )
    r = client.post(f"/scribble/engagements/by-core/{anchor}/promote-findings/job-1",
                    data={"finding_ids": [str(f1), str(uuid.uuid7())]})
    assert r.status_code == 404
