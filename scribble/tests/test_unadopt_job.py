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


def _engagement(session_factory, name: str = "E"):
    with session_factory() as db:
        eng = fm.ReportBoard(name=name)  # client_id NULL -> admin-only, which the default stub actor is
        db.add(eng)
        db.commit()
        # (id, core-engagement anchor #845) — an adopted job must carry the SAME engagement_id or the
        # promote guard refuses it 409. `expire_on_commit=False`, so the anchor is readable post-commit.
        return eng.id, eng.core_engagement_id


def _adopt(client, stub_host, eid, anchor, job_id="job-x", dtos=()):
    """Register a viewable job ANCHORED to this board (#845) and drive #630's adopt route: LINK + pour its
    findings onto the board."""
    stub_host.findings.add_job(job_id, owner_id=1, dtos=list(dtos), engagement_id=anchor)
    resp = client.post(f"/scribble/engagements/{eid}/adopt-job/{job_id}")
    assert resp.status_code in (302, 303), resp.data
    return job_id


def _finding_ids(session_factory, eid):
    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        return {(f.title, f.source_finding_id) for f in eng.findings}


def _enriched_row_ids(session_factory, eid, source_ids):
    """The scribble BoardFinding PKs (as str) whose source finding id is in ``source_ids`` — the
    rows a destructive un-adopt of that job removes, identified by their OWN id (not the core one)."""
    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        return {str(f.id) for f in eng.findings if f.source_finding_id in source_ids}


# ── (a) link-only ────────────────────────────────────────────────────────────────────────────────

def test_unadopt_link_only_clears_link_but_keeps_findings(client, stub_host, session_factory, clean_vuln_map):
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x",
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
    eid, _ = _engagement(session_factory)
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
    mine, _ = _engagement(session_factory, "mine")
    other, other_anchor = _engagement(session_factory, "other")
    _adopt(client, stub_host, other, other_anchor, "job-y",
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
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE"), FakeFindingDTO(id=102, title="XSS")])
    # A hand-authored finding (no source finding id) — this job did NOT enrich it.
    with session_factory() as db:
        db.add(fm.BoardFinding(engagement_id=eid, title="Manual note", severity=Severity.info))
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
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE"), FakeFindingDTO(id=102, title="XSS")])
    with session_factory() as db:
        db.add(fm.BoardFinding(engagement_id=eid, title="Manual note", severity=Severity.info))
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
    eid, _ = _engagement(session_factory)
    # The ACT is write-gated...
    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy").status_code == 403
    # ...but the preview is a GET, so a writeless viewer may read it (tenancy contract: GET == view).
    assert client.get(f"/scribble/engagements/{eid}/unadopt-job/job-x/preview").status_code == 200


# ── the panel renders the affordance (additive to #629/#630) ────────────────────────────────────────

def test_panel_renders_unadopt_controls_for_each_source_job(client, stub_host, session_factory):
    eid, _ = _engagement(session_factory)
    stub_host.add_promoted_job(eid, "job-alpha")
    body = client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)
    assert "Source jobs" in body                                  # #629 panel intact
    assert 'id="scribble-adopt-job-btn"' in body                  # #630 picker intact
    assert "/unadopt-job/job-alpha" in body                       # link-only affordance wired
    assert "/unadopt-job/job-alpha/preview" in body               # destructive preview wired
    assert "/unadopt-job/job-alpha/destroy" in body               # destructive destroy wired


# ── (c) the destructive path does not depend on the LINK still being present (#402) ────────────────

def test_destructive_unadopt_still_deletes_after_the_link_is_gone(
    client, stub_host, session_factory, clean_vuln_map
):
    """#402: "Un-adopt + delete" left the whole block on the board.

    `_is_source_job` (the reverse index) is about not clearing ANOTHER engagement's link. It was also
    gating the whole route, so once the link was gone -- a prior link-only un-adopt, or an adopt that
    poured findings and then failed before marking the link -- the destroy returned "success" having
    deleted nothing. The delete is already engagement-scoped (enriched_findings reads THIS board's rows),
    so the guard must scope only the link clear.
    """
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE"), FakeFindingDTO(id=102, title="XSS")])
    # The link goes away FIRST (the operator's link-only un-adopt, or a failed adopt's aftermath).
    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x").status_code in (302, 303)
    assert _finding_ids(session_factory, eid) == {("RCE", 101), ("XSS", 102)}

    resp = client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy")
    assert resp.status_code in (302, 303), resp.data

    # The block is gone: the destroy must not be a silent no-op just because the link was cleared.
    assert _finding_ids(session_factory, eid) == set(), "the adopted block survived the destroy"
    actions = [a for a in stub_host.audit_calls if a[0] == "ext:scribble:unadopt_job_destructive"]
    assert len(actions) == 1, stub_host.audit_calls

def test_the_panel_still_offers_the_destroy_after_the_link_is_gone(
    client, stub_host, session_factory, clean_vuln_map
):
    """#402 review: the controls render from the LINK-derived list, so once the link is gone the operator
    had no path to the route this PR fixes. The panel must also list jobs that still OWN rows here."""
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x", dtos=[FakeFindingDTO(id=101, title="RCE")])
    # The adopt path writes the job's coverage note, which is the ONE row carrying source_job_id
    # (coverage.py); it is what tells the panel this job still owns rows here.
    with session_factory() as db:
        db.add(fm.BoardFinding(engagement_id=eid, title="Scan coverage: job job-x",
                               severity=Severity.info, source_job_id="job-x"))
        db.commit()
    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x").status_code in (302, 303)

    body = client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)
    assert "/unadopt-job/job-x/destroy" in body, "no control reaches the destroy route any more"
    assert "/unadopt-job/job-x/preview" in body

def test_destructive_unadopt_removes_the_jobs_coverage_note(
    client, stub_host, session_factory, clean_vuln_map
):
    """#402 review r2: the #656 coverage note is job-derived (source_job_id set, source_finding_id NULL).

    Leaving it behind after the destroy says "job X was promoted into this engagement" about a board
    whose rows are gone. It is selected by source_job_id, and an author-owned row that merely mentions
    the job id in its TITLE must survive.
    """
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x", dtos=[FakeFindingDTO(id=101, title="RCE")])
    with session_factory() as db:
        db.add(fm.BoardFinding(engagement_id=eid, title="Scan coverage: job job-x",
                               severity=Severity.info, source_job_id="job-x"))
        # An author row whose title mentions the job id but which carries no source id: it must survive.
        db.add(fm.BoardFinding(engagement_id=eid, title="Manual note about job job-x",
                               severity=Severity.info))
        db.commit()

    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy").status_code in (302, 303)

    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        titles = {f.title for f in eng.findings}
    assert "RCE" not in titles, "the poured finding survived"
    assert "Scan coverage: job job-x" not in titles, "the coverage note survived the destroy"
    assert titles == {"Manual note about job job-x"}, titles


# ── the emptied section (#402: no stale auto section, never a hand-authored one) ─────────────────────

def _sections(session_factory, eid) -> dict[str, fm.FindingGroup]:
    with session_factory() as db:
        return {g.name: g for g in db.get(fm.ReportBoard, eid).groups}


def _destroyed_sections(stub_host) -> list[str]:
    """The section names the LAST destructive un-adopt recorded in its audit row. Kept as a helper so
    every destroy assertion names the field the same way, and so a renamed audit field fails here once
    rather than in five places."""
    actions = [a for a in stub_host.audit_calls if a[0] == "ext:scribble:unadopt_job_destructive"]
    assert actions, "the destructive un-adopt wrote no audit row at all"
    return actions[-1][1]["before"]["removed_section_names"]


def _machine_section(session_factory, eid, job_id, *, scope_type="external") -> str:
    """Create the section the ADOPT path would create for ``job_id`` and move its rows in -- by calling
    the SAME `findings_service.auto_section_job_findings` the adopt route calls, so the provenance flag
    and the section name are the product's, not the test's. (The stub host's job carries no
    ``scope_type``, which is why the adopt route itself sections nothing in this fixture.) Returns the
    section id; asserts the section really is the machine's."""
    from scribble import findings_service
    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        findings_service.auto_section_job_findings(db, eng, job_id=job_id, scope_type=scope_type)
        db.commit()
    with session_factory() as db:
        sections = [g for g in db.get(fm.ReportBoard, eid).groups]
        assert len(sections) == 1, [g.name for g in sections]
        section = sections[0]
        assert section.machine_authored is True, "premise: the adopt path marks what it creates"
        return section.id


def _put_rows_in(session_factory, eid, group_id, *, titles):
    """Move the board rows named by ``titles`` into ``group_id`` (the operator's drag-drop effect)."""
    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        moved = 0
        for finding in eng.findings:
            if finding.title in titles:
                finding.group_id = group_id
                moved += 1
        db.commit()
        assert moved == len(titles), f"{moved} of {len(titles)} rows moved"


def test_destructive_unadopt_removes_the_emptied_auto_section(
    client, stub_host, session_factory, clean_vuln_map
):
    """#402's contract: "An empty automatically-created section should not appear as a stale job block.
    Do not delete a manually authored section silently." — BOTH sides in one run.

    The adopt path sections a job automatically, so a destroy that empties that section would otherwise
    leave a bare "External" card on the board forever. Here the auto section holds ONLY job-x's rows and
    is emptied by the destroy, so it must go; a hand-authored section holding a surviving finding must
    stay. Revert `delete_auto_section_if_empty` to a no-op and the first assert goes RED; drop its
    `machine_authored` check and the second does.
    """
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE"), FakeFindingDTO(id=102, title="XSS")])
    with session_factory() as db:
        auto = fm.FindingGroup(engagement_id=eid, name="External", order_index=0,
                               machine_authored=True)
        manual = fm.FindingGroup(engagement_id=eid, name="Operator Notes", order_index=1)
        db.add_all([auto, manual])
        db.flush()
        auto_id, manual_id = auto.id, manual.id
        db.add(fm.BoardFinding(engagement_id=eid, title="Operator note", severity=Severity.info,
                              group_id=manual_id))
        db.commit()
    # (adopt wrote no coverage note here: it was ACKNOWLEDGED and its coverage was measured)
    _put_rows_in(session_factory, eid, auto_id, titles=["RCE", "XSS"])
    assert set(_sections(session_factory, eid)) == {"External", "Operator Notes"}

    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy").status_code in (302, 303)

    after = _sections(session_factory, eid)
    assert "External" not in after, "the emptied auto-created section survived as a stale job block"
    assert "Operator Notes" in after, "a hand-authored section was deleted silently"
    with session_factory() as db:
        assert {f.title for f in db.get(fm.ReportBoard, eid).findings} == {"Operator note"}
    # The audit row must NAME what went (Plague r2): for a delete, the trail is the only record of it.
    assert _destroyed_sections(stub_host) == ["External"]


def test_destructive_unadopt_keeps_an_auto_section_that_still_holds_findings(
    client, stub_host, session_factory, clean_vuln_map
):
    """The two-job SHARED section: clearing job-x out of one section must not take the section (nor
    job-y's rows in it) with it. Only an EMPTY auto section is removable."""
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x", dtos=[FakeFindingDTO(id=101, title="RCE")])
    with session_factory() as db:
        auto = fm.FindingGroup(engagement_id=eid, name="External", order_index=0,
                               machine_authored=True)
        db.add(auto)
        db.flush()
        auto_id = auto.id
        # A row this destroy does not select, sitting in the same shared section.
        db.add(fm.BoardFinding(engagement_id=eid, title="Other job row", severity=Severity.high,
                              group_id=auto_id, source_finding_id=999))
        db.commit()
    _put_rows_in(session_factory, eid, auto_id, titles=["RCE"])

    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy").status_code in (302, 303)

    after = _sections(session_factory, eid)
    assert "External" in after, "a shared section was deleted while another job's row still used it"
    assert after["External"].machine_authored is True, "provenance was rewritten"
    with session_factory() as db:
        assert {f.title for f in db.get(fm.ReportBoard, eid).findings} == {"Other job row"}
    # ...and the audit says NOTHING was removed, so a reader cannot mistake this for a prune.
    assert _destroyed_sections(stub_host) == []


def test_destructive_unadopt_never_removes_a_hand_authored_section_even_when_emptied(
    client, stub_host, session_factory, clean_vuln_map
):
    """An operator can drag an adopted row into a section THEY made. Emptying it must not delete it:
    #402 says a manually authored section is never removed silently — "empty" alone is not consent."""
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x", dtos=[FakeFindingDTO(id=101, title="RCE")])
    with session_factory() as db:
        manual = fm.FindingGroup(engagement_id=eid, name="Client Narrative", order_index=0)
        db.add(manual)
        db.flush()
        manual_id = manual.id
        db.commit()
    _put_rows_in(session_factory, eid, manual_id, titles=["RCE"])

    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy").status_code in (302, 303)

    after = _sections(session_factory, eid)
    assert "Client Narrative" in after, "a hand-authored section was deleted because it went empty"
    # ...and the board still renders it rather than erroring on an empty group.
    assert client.get(f"/scribble/engagements/{eid}").status_code == 200


# ── (d) a HUMAN taking ownership of a machine section (Plague r2, ext#402) ────────────────────────


def test_destructive_unadopt_keeps_a_machine_section_the_operator_renamed(
    client, stub_host, session_factory, clean_vuln_map
):
    """The measured r2 harm, end to end: adopt -> the path auto-creates `External` -> the operator
    renames it to `Web Application Findings` on the board -> destroy the only job. The section must
    SURVIVE: a person chose its name, so it is not "a section nobody chose" any more, and #402's
    "do not delete a manually authored section silently" covers it.

    RED on the pre-fix tree: the rename left `machine_authored` True, so the destroy pruned it and the
    board came back with no sections at all.
    """
    eid, anchor = _engagement(session_factory)
    # `job_id` on each DTO is what `source_facts` snapshots, and therefore what
    # `auto_section_job_findings` keys on -- the DTOs in this file's other tests omit it because they
    # hand-build their section instead.
    _adopt(client, stub_host, eid, anchor, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE", job_id="job-x"),
                 FakeFindingDTO(id=102, title="XSS", job_id="job-x")])
    section_id = _machine_section(session_factory, eid, "job-x")
    assert set(_sections(session_factory, eid)) == {"External"}

    # The board's own name input: the cookie route, not a service call.
    renamed = client.post(f"/scribble/api/groups/{section_id}",
                          json={"name": "Web Application Findings"})
    assert renamed.status_code == 200, renamed.get_json()
    assert "Web Application Findings" in _sections(session_factory, eid)

    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy").status_code in (302, 303)

    after = _sections(session_factory, eid)
    assert set(after) == {"Web Application Findings"}, (
        "the destroy discarded a section the operator named -- it is only visible in the audit row"
    )
    assert after["Web Application Findings"].machine_authored is False
    # The job's rows still went (that is what destroy means); only the human-owned section stayed.
    assert _finding_ids(session_factory, eid) == set()
    assert _destroyed_sections(stub_host) == [], "the audit claims a section went that did not"


def test_a_report_excluded_machine_section_stays_excluded_across_destroy_and_readopt(
    client, stub_host, session_factory, clean_vuln_map
):
    """The worse r2 harm, end to end: the operator sets `include_in_report=False` on the auto section (a
    CLIENT DELIVERABLE decision), un-adopts the only job, later re-adopts it. Pre-fix the destroy pruned
    the section and `create_group` re-created it with the default `include_in_report=True`, silently
    putting an excluded section BACK into the client report. With the human edit recorded, the section is
    not pruned, so the re-adopt REUSES it and the exclusion survives.

    RED on the pre-fix tree at the first `after` assertion (the section is gone) and at
    `include_in_report` after the re-adopt (True).
    """
    eid, anchor = _engagement(session_factory)
    _adopt(client, stub_host, eid, anchor, "job-x",
           dtos=[FakeFindingDTO(id=101, title="RCE", job_id="job-x")])
    section_id = _machine_section(session_factory, eid, "job-x")

    excluded = client.post(f"/scribble/api/groups/{section_id}", json={"include_in_report": False})
    assert excluded.status_code == 200, excluded.get_json()
    assert excluded.get_json()["include_in_report"] is False

    assert client.post(f"/scribble/engagements/{eid}/unadopt-job/job-x/destroy").status_code in (302, 303)
    after = _sections(session_factory, eid)
    assert set(after) == {"External"}, "the destroy pruned a section whose report inclusion a person set"
    assert after["External"].include_in_report is False

    # The reverse of the destroy: re-adopt the SAME job (its findings are re-poured from the host's view).
    assert client.post(f"/scribble/engagements/{eid}/adopt-job/job-x").status_code in (302, 303)
    reafter = _sections(session_factory, eid)
    assert set(reafter) == {"External"}, "a second External section was created beside the excluded one"
    assert reafter["External"].include_in_report is False, (
        "re-adopt silently re-included a section the operator excluded from the client report"
    )
    assert reafter["External"].machine_authored is False
