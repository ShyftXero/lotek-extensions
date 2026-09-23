"""Adopt-a-job UI (#630): the browser action that LINKS a scan job into this engagement.

`promote-job` pours a scan job's findings onto the board; adopting is the same act named for the operator
choosing a job in the Source-jobs picker (#629 panel). The link itself is `host.mark_job_promoted`, which
is REFUSE-ON-CONFLICT (core #632): a job already promoted into a DIFFERENT engagement is refused, never
silently re-pointed. This asserts the END-STATE the browser reaches (repo rule "test the UI contract, not
just the API"): a POST to the adopt route on an unadopted job links it and the Source-jobs panel then
lists it; a POST on a job already promoted elsewhere returns 409 and leaves BOTH sides unchanged.

RED before the route exists (404) and before the stub models conflict (no 409).
"""
from __future__ import annotations

import uuid

import scribble.models as fm
from tests.conftest import FakeFindingDTO


def _engagement(session_factory, name: str = "E"):
    with session_factory() as db:
        eng = fm.ReportBoard(name=name)  # client_id NULL -> admin-only, which the default stub actor is
        db.add(eng)
        db.commit()
        # (id, core-engagement anchor #845) — an adopted job must carry the SAME engagement_id or the
        # promote guard refuses it 409. `expire_on_commit=False`, so the anchor is readable post-commit.
        return eng.id, eng.core_engagement_id


def test_adopt_links_an_unadopted_job(client, stub_host, session_factory):
    eid, anchor = _engagement(session_factory)
    stub_host.findings.add_job("job-x", owner_id=1, dtos=[], engagement_id=anchor)  # admin may view it

    resp = client.post(f"/scribble/engagements/{eid}/adopt-job/job-x")
    assert resp.status_code in (302, 303), resp.data  # redirects back to the board

    body = client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)
    assert "job-x" in body  # the panel now lists the adopted job (via host.list_jobs)


def test_adopt_already_promoted_elsewhere_is_409_and_does_not_repoint(client, stub_host, session_factory):
    mine, mine_anchor = _engagement(session_factory, "mine")
    other, _ = _engagement(session_factory, "other")
    # Anchor job-y to MINE so the #845 check passes and the 409 comes from refuse-on-conflict (the intent
    # under test), not from a cross-engagement mismatch.
    stub_host.findings.add_job("job-y", owner_id=1, dtos=[], engagement_id=mine_anchor)
    stub_host.add_promoted_job(other, "job-y")  # already linked into a DIFFERENT engagement

    resp = client.post(f"/scribble/engagements/{mine}/adopt-job/job-y")
    assert resp.status_code == 409, resp.data  # refuse-on-conflict surfaced, not swallowed

    # Not re-pointed: the conflicting engagement still owns it, this one still shows nothing.
    assert "job-y" not in client.get(f"/scribble/engagements/{mine}").get_data(as_text=True)
    assert "job-y" in client.get(f"/scribble/engagements/{other}").get_data(as_text=True)


def test_board_renders_adopt_picker_alongside_the_panel(client, stub_host, session_factory):
    """The picker is ADDITIVE to #629's Source-jobs panel: the panel copy is still there AND the adopt
    control renders (guards the template edit + that it didn't clobber the prior link's panel)."""
    eid, _ = _engagement(session_factory)
    body = client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)
    assert "Source jobs" in body                       # #629 panel intact
    assert 'id="scribble-adopt-job-btn"' in body       # #630 picker present
    assert "/adopt-job/__JOBID__" in body              # wired to the adopt route


def test_adopt_unknown_job_is_silent_noop_no_leak(client, stub_host, session_factory):
    """A job the actor can't see / doesn't exist is a silent no-op redirect (not-found and not-viewable
    indistinguishable), same posture the promote twin gives -- and nothing is linked. It must NOT 404,
    which the engagement-scope tenancy gate reserves as its OWN denial signal."""
    eid, _ = _engagement(session_factory)
    resp = client.post(f"/scribble/engagements/{eid}/adopt-job/ghost")
    assert resp.status_code in (302, 303), resp.data
    assert not stub_host.promoted_calls  # nothing was linked
    assert "ghost" not in client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)


def test_adopt_refuses_cross_engagement_before_marking(client, stub_host, session_factory):
    """The #845 anchor check runs BEFORE the refuse-on-conflict mark: a job anchored to a DIFFERENT core
    engagement aborts 409, `host.mark_job_promoted` is NEVER called (promoted_calls stays empty), and the
    job is left UNLINKED — so a cross-engagement adopt can't leave a job linked-but-not-poured.

    Non-vacuous: move the anchor check below the mark (or drop it) and mark_job_promoted records the
    attempt — promoted_calls is non-empty and the job would be linked."""
    eid, _ = _engagement(session_factory)
    stub_host.findings.add_job(
        "job-z", owner_id=1, dtos=[FakeFindingDTO(id=1, title="SQLi")], engagement_id=uuid.uuid7()
    )

    resp = client.post(f"/scribble/engagements/{eid}/adopt-job/job-z")
    assert resp.status_code == 409, resp.data
    assert stub_host.promoted_calls == []  # the gating mark never ran — anchor check is first
    # nothing linked (panel) or poured (board)
    assert "job-z" not in client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)
    with session_factory() as db:
        assert list(db.get(fm.ReportBoard, eid).findings) == []
