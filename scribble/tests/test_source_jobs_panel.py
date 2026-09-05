"""Engagement board "Source jobs" panel (#629): the REVERSE of promotion.

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
        eng = fm.Engagement(name=name)  # client_id NULL -> admin-only, which the default stub actor is
        db.add(eng)
        db.commit()
        return eng.id


def test_board_lists_jobs_promoted_into_this_engagement(client, stub_host, session_factory):
    eid = _engagement(session_factory)
    when = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
    stub_host.add_promoted_job(eid, "job-alpha", promoted_at=when)
    stub_host.add_promoted_job(eid, "job-bravo", promoted_at=when)

    resp = client.get(f"/scribble/engagements/{eid}")
    assert resp.status_code == 200, resp.data
    body = resp.get_data(as_text=True)

    assert "Source jobs" in body            # the panel header exists
    assert "job-alpha" in body              # both job refs render
    assert "job-bravo" in body
    assert "2026-09-04 13:30" in body       # ...each with its promoted_at


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
