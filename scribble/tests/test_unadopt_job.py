"""Un-adopt semantics (#635): the reverse of adopt-a-job (#630), with TWO paths.

`adopt-job` LINKS a scan job into an engagement (`host.mark_job_promoted`) AND pours its findings onto
the board (`promote_job`). Un-adopt undoes it, and the operator chooses how much:

  * LINK-ONLY (`unadopt-job`)      -> `host.remove_job_adoption`: drop the link, keep every finding.
  * DESTRUCTIVE (`.../destroy`)    -> also delete EXACTLY the findings that job enriched, gated behind a
                                      PREVIEW (`.../preview`, lists the ids first) and an audit row.

These assert the END-STATE the caller reaches (repo rule "test the UI contract, not just the API"):
route status + the rendered panel / preview JSON / surviving DB rows given stub data. RED before the
routes exist (404) and before `promote.enriched_findings` exists (ImportError).
"""
from __future__ import annotations

import scribble.models as fm
from scribble.enums import Severity
from tests.conftest import FakeFindingDTO


def _engagement(session_factory, name: str = "E") -> object:
    with session_factory() as db:
        eng = fm.Engagement(name=name)  # client_id NULL -> admin-only, which the default stub actor is
        db.add(eng)
        db.commit()
        return eng.id


def _adopt(client, stub_host, eid, job_id="job-x", dtos=()):
    """Register a viewable job and drive #630's adopt route: LINK + pour its findings onto the board."""
    stub_host.findings.add_job(job_id, owner_id=1, dtos=list(dtos))
    resp = client.post(f"/scribble/engagements/{eid}/adopt-job/{job_id}")
    assert resp.status_code in (302, 303), resp.data
    return job_id


def _finding_ids(session_factory, eid):
    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        return {(f.title, f.source_finding_id) for f in eng.findings}


def _enriched_row_ids(session_factory, eid, source_ids):
    """The scribble EngagementFinding PKs (as str) whose source finding id is in ``source_ids`` — the
    rows a destructive un-adopt of that job removes, identified by their OWN id (not the core one)."""
    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        return {str(f.id) for f in eng.findings if f.source_finding_id in source_ids}


# ── (a) link-only ────────────────────────────────────────────────────────────────────────────────

def test_unadopt_link_only_clears_link_but_keeps_findings(client, stub_host, session_factory, clean_vuln_map):
    eid = _engagement(session_factory)
    _adopt(client, stub_host, eid, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE"), FakeFindingDTO(id=102, title="XSS")])
    # Poured: two findings, each carrying its source finding id; the panel lists the job.
    assert _finding_ids(session_factory, eid) == {("RCE", 101), ("XSS", 102)}
    assert "job-x" in client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)

    resp = client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x")
    assert resp.status_code in (302, 303), resp.data

    # Link gone from the panel...
    assert "job-x" not in client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)
    # ...but every finding it poured is untouched, and the host was asked to clear the LINK only,
    # for the session actor (same identity adopt_job authorizes with).
    assert _finding_ids(session_factory, eid) == {("RCE", 101), ("XSS", 102)}
    assert [c[0] for c in stub_host.unadopt_calls] == ["job-x"]
    assert stub_host.unadopt_calls[0][1] is stub_host.current_user


def test_unadopt_link_only_requires_write(client, stub_host, session_factory):
    stub_host.can_write_value = False
    eid = _engagement(session_factory)
    resp = client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x")
    assert resp.status_code == 403


def test_unadopt_unknown_engagement_404(client, stub_host):
    import uuid
    resp = client.post(f"/scribble/engagements/{uuid.uuid7()}/unadopt-job/job-x")
    assert resp.status_code == 404


def test_unadopt_via_wrong_engagement_does_not_clear_another_engagements_link(
    client, stub_host, session_factory, clean_vuln_map
):
    """`host.remove_job_adoption` takes only a job id, so an un-adopt POSTed at the WRONG engagement's URL
    must not clear a job linked to a DIFFERENT engagement. Both paths are scoped to `host.list_jobs`."""
    mine = _engagement(session_factory, "mine")
    other = _engagement(session_factory, "other")
    _adopt(client, stub_host, other, "job-y",
           dtos=[FakeFindingDTO(id=201, title="RCE")])  # job-y belongs to OTHER
    assert "job-y" in client.get(f"/scribble/engagements/{other}").get_data(as_text=True)

    # Link-only un-adopt via MINE: no-op, OTHER keeps its link, the host was never asked to clear it.
    assert client.post(f"/scribble/engagements/{mine}/unadopt-job/job-y").status_code in (302, 303)
    assert not stub_host.unadopt_calls
    assert "job-y" in client.get(f"/scribble/engagements/{other}").get_data(as_text=True)

    # Destructive un-adopt via MINE: no-op too — OTHER's finding survives, nothing audited.
    assert client.post(f"/scribble/engagements/{mine}/unadopt-job/job-y/destroy").status_code in (302, 303)
    assert not stub_host.unadopt_calls
    assert _finding_ids(session_factory, other) == {("RCE", 201)}
    assert not [a for a in stub_host.audit_calls if a[0] == "ext:scribble:unadopt_job_destructive"]


# ── (b) destructive: preview then confirm ──────────────────────────────────────────────────────────

def test_destructive_preview_lists_exactly_the_enriched_findings(
    client, stub_host, session_factory, clean_vuln_map
):
    eid = _engagement(session_factory)
    _adopt(client, stub_host, eid, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE"), FakeFindingDTO(id=102, title="XSS")])
    # A hand-authored finding (no source finding id) — this job did NOT enrich it.
    with session_factory() as db:
        db.add(fm.EngagementFinding(engagement_id=eid, title="Manual note", severity=Severity.info))
        db.commit()

    resp = client.get(f"/scribble/engagements/{eid}/unadopt-job/job-x/preview")
    assert resp.status_code == 200, resp.data
    payload = resp.get_json()
    # Exactly the two findings the job enriched — the manual note is NOT listed.
    assert {f["title"] for f in payload["findings"]} == {"RCE", "XSS"}
    # ...and the ids are those rows' OWN scribble PKs (what the destroy route will delete).
    assert {f["id"] for f in payload["findings"]} == _enriched_row_ids(session_factory, eid, {101, 102})


def test_destructive_confirm_removes_exactly_those_and_audits(
    client, stub_host, session_factory, clean_vuln_map
):
    eid = _engagement(session_factory)
    _adopt(client, stub_host, eid, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE"), FakeFindingDTO(id=102, title="XSS")])
    with session_factory() as db:
        db.add(fm.EngagementFinding(engagement_id=eid, title="Manual note", severity=Severity.info))
        db.commit()
    # Capture the enriched rows' OWN ids BEFORE the destroy — that's what should be deleted + audited.
    doomed_ids = _enriched_row_ids(session_factory, eid, {101, 102})
    assert len(doomed_ids) == 2

    resp = client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy")
    assert resp.status_code in (302, 303), resp.data

    # Exactly the enriched rows are gone; the hand-authored one survives.
    assert _finding_ids(session_factory, eid) == {("Manual note", None)}
    # The link was cleared too.
    assert "job-x" not in client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)
    # One audit row records what went, keyed by exactly the removed rows' ids.
    actions = [a for a in stub_host.audit_calls if a[0] == "ext:scribble:unadopt_job_destructive"]
    assert len(actions) == 1, stub_host.audit_calls
    before = actions[0][1]["before"]
    assert before["job_id"] == "job-x"
    assert set(before["removed_finding_ids"]) == doomed_ids


def test_destructive_destroy_requires_write_but_preview_is_a_viewable_get(client, stub_host, session_factory):
    stub_host.can_write_value = False
    eid = _engagement(session_factory)
    # The ACT is write-gated...
    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy").status_code == 403
    # ...but the preview is a GET, so a writeless viewer may read it (tenancy contract: GET == view).
    assert client.get(f"/scribble/engagements/{eid}/unadopt-job/job-x/preview").status_code == 200


# ── the panel renders the affordance (additive to #629/#630) ────────────────────────────────────────

def test_panel_renders_unadopt_controls_for_each_source_job(client, stub_host, session_factory):
    eid = _engagement(session_factory)
    stub_host.add_promoted_job(eid, "job-alpha")
    body = client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)
    assert "Source jobs" in body                                  # #629 panel intact
    assert 'id="scribble-adopt-job-btn"' in body                  # #630 picker intact
    assert "/unadopt-job/job-alpha" in body                       # link-only affordance wired
    assert "/unadopt-job/job-alpha/preview" in body               # destructive preview wired
    assert "/unadopt-job/job-alpha/destroy" in body               # destructive destroy wired
