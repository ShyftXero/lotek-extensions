"""First-job empty-state CTA (#631): when NO scan job has been promoted into this engagement, the
Source-jobs panel (#629) shows a call-to-action that deep-links to the CORE job-create page, so an
operator can run the first scan for this fresh engagement.

Additive to #629/#630 — an engagement WITH source jobs shows no empty-state CTA. This asserts the
END-STATE the browser reaches (repo rule "test the UI contract, not just the API"): the rendered board,
not an internal call. RED before the CTA markup exists in `engagement.html`.
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


def test_empty_engagement_renders_first_job_cta(client, stub_host, session_factory):
    eid = _engagement(session_factory)
    body = client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)

    assert "scribble-source-jobs-cta" in body            # the empty-state CTA renders
    assert 'href="/assessments/new"' in body             # ...deep-linking to the core job-create page


def test_engagement_with_jobs_hides_the_cta(client, stub_host, session_factory):
    eid = _engagement(session_factory)
    stub_host.add_promoted_job(eid, "job-alpha", promoted_at=datetime.now(UTC))

    body = client.get(f"/scribble/engagements/{eid}").get_data(as_text=True)

    assert "job-alpha" in body                           # the promoted job renders...
    assert "scribble-source-jobs-cta" not in body        # ...and the empty-state CTA is gone
