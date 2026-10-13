"""ReportBoard board "Source jobs" panel (#629): the REVERSE of promotion.

`promote-job` records that a host scan job's findings were poured onto THIS engagement
(`host.mark_job_promoted`). The reverse view — which jobs fed this engagement — is `host.list_jobs`
(core #632). This asserts the END-STATE the browser reaches: a GET of the engagement board renders one
row per promoted job (its ref + when it was promoted), and an explicit empty state when none exist.

Asserting the rendered panel, not just an internal call, is deliberate (repo rule "test the UI contract,
not just the API"): removing the panel from `engagement.html`, or dropping `source_jobs` from the render
context, turns these RED. The panel is the insertion point #630/#631/#635 build richer content onto.
"""
from __future__ import annotations

from datetime import UTC, datetime

import scribble.models as fm


def _engagement(session_factory, name: str = "E") -> object:
    with session_factory() as db:
        eng = fm.ReportBoard(name=name)  # client_id NULL -> admin-only, which the default stub actor is
        db.add(eng)
        db.commit()
        return eng.id


def test_board_lists_jobs_promoted_into_this_engagement(client, stub_host, session_factory):
    eid = _engagement(session_factory)
    when = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
    stub_host.add_promoted_job(eid, "job-alpha", name="ukg-ext-web", promoted_at=when)
    stub_host.add_promoted_job(eid, "job-bravo", promoted_at=when)

    resp = client.get(f"/scribble/engagements/{eid}")
    assert resp.status_code == 200, resp.data
    body = resp.get_data(as_text=True)

    assert "Source jobs" in body            # the panel header exists
    assert "ukg-ext-web" in body            # the user-friendly job NAME renders, not the raw id
    assert "job-bravo" in body              # name defaults to the id when unnamed
    assert '/jobs/job-alpha' in body        # ...each linked to the core job view (#629 name+link)
    assert "2026-09-04 13:30" in body       # ...each with its promoted_at
    # Re-map (regroup) action is offered per source job (writer context) so the operator can re-run the
    # vuln-map after it grows — the button posts to the non-destructive remap route.
    assert "/remap-job/job-alpha" in body
    assert "Re-map (regroup)" in body


def test_board_source_jobs_empty_state_when_none(client, stub_host, session_factory):
    eid = _engagement(session_factory)

    resp = client.get(f"/scribble/engagements/{eid}")
    assert resp.status_code == 200, resp.data
    body = resp.get_data(as_text=True)

    assert "Source jobs" in body                          # panel still present (sets up #631)
    assert "No scan jobs promoted into this engagement" in body


def test_board_source_jobs_scoped_to_this_engagement(client, stub_host, session_factory):
    """A job promoted into a DIFFERENT engagement must not bleed into this one's panel — the reverse
    index is keyed by ref_id, so the wrong engagement id returns nothing."""
    mine = _engagement(session_factory, "mine")
    other = _engagement(session_factory, "other")
    stub_host.add_promoted_job(other, "job-elsewhere", promoted_at=datetime.now(UTC))

    body = client.get(f"/scribble/engagements/{mine}").get_data(as_text=True)
    assert "job-elsewhere" not in body
    assert "No scan jobs promoted into this engagement" in body


def test_destroy_button_js_carries_a_csrf_token():
    """The un-adopt+delete button builds a form in JS and calls form.submit(), which does NOT fire the
    submit event the host's global CSRF-injector hooks. Without an explicit token the POST fails CSRF and
    the error handler bounces the browser to /login (looks like a logout — the prod bug behind #635).
    Route tests use the test client, which bypasses CSRF, so only this static guard catches a regression
    to a bare form.submit(). If the token injection is removed, this goes red."""
    import pathlib
    board_js = (pathlib.Path(__file__).parent.parent / "scribble" / "static" / "board.js").read_text()
    # the destroy handler must append a hidden csrf_token to the form it submits
    destroy = board_js[board_js.index("initDestructiveUnadopt"):]
    assert 'name = "csrf_token"' in destroy or "name = 'csrf_token'" in destroy, (
        "destroy form submits without a CSRF token -> prod logout bug"
    )
