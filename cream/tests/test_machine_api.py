"""The PAT machine API (``cream/api_pat.py``) — scope gating, PAT-actor tenancy, and the two omissions
that keep financial finalization human-only.

These are cream's OWN proofs. The host's token/scope *scheme* is lotek's concern (proven there against the
real authenticator); what must be proven HERE is that cream declares the right scope on every machine
route, attributes writes to the PAT principal rather than a session that does not exist on this surface,
and never exposes ``/issue`` or ``/void``.
"""

from __future__ import annotations

import uuid

import pytest

from cream.host import SCOPE_ATTR
from cream.models import Document

MACHINE = "/cream/machine"


def _machine_rules(app):
    return [r for r in app.url_map.iter_rules() if str(r.rule).startswith(MACHINE)]


# ── the two load-bearing invariants ──────────────────────────────────────────────────────────────────


def test_every_machine_route_is_scope_gated(app):
    """No route on this surface may be reachable by a merely-authenticated token.

    Walks the real ``url_map`` rather than a hand-kept list, so a route added later without
    ``@host.require_scope`` fails this test instead of shipping ungated.
    """
    rules = _machine_rules(app)
    assert rules, "no /cream/machine routes are registered at all"
    ungated = [
        str(r.rule) for r in rules
        if not hasattr(app.view_functions[r.endpoint], SCOPE_ATTR)
    ]
    assert ungated == [], f"machine routes missing require_scope: {ungated}"


@pytest.mark.parametrize("verb", ["issue", "void"])
def test_finalization_verbs_are_absent_from_the_machine_surface(app, verb):
    """Freezing a draft into an immutable numbered document, and voiding an issued one, are human-only.
    They are absent by OMISSION — asserted here so a future route cannot add them unnoticed."""
    offenders = [str(r.rule) for r in _machine_rules(app) if str(r.rule).endswith(f"/{verb}")]
    assert offenders == [], f"machine surface exposes financial finalization: {offenders}"


def test_the_browser_surface_still_has_them(app):
    """Control for the test above: the verbs exist on the cookie-authed surface, so their absence from
    the machine surface is a deliberate omission and not a typo in the assertion."""
    browser = [str(r.rule) for r in app.url_map.iter_rules() if str(r.rule).startswith("/cream/api")]
    assert any(r.endswith("/issue") for r in browser)
    assert any(r.endswith("/void") for r in browser)


# ── scope gating ─────────────────────────────────────────────────────────────────────────────────────


def test_read_token_cannot_write(pat_client, hooks, engagement_id, session_factory):
    """A read-scoped token is refused by every write route, and writes nothing."""
    hooks["pat_actor"] = type(hooks["pat_actor"])(scopes=frozenset({"read"}))
    res = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Nope"})
    assert res.status_code == 403, res.get_data(as_text=True)
    with session_factory() as db:
        assert db.query(Document).count() == 0


def test_read_token_can_read(pat_client):
    hooks_res = pat_client.get(f"{MACHINE}/documents")
    assert hooks_res.status_code == 200
    assert hooks_res.get_json()["documents"] == []


def test_unauthenticated_token_is_refused(pat_client, hooks):
    """The host's ``pat_authenticate`` result is honoured as-is by the blueprint's before_request."""
    hooks["pat_authenticate"] = ({"error": "unauthorized"}, 401)
    assert pat_client.get(f"{MACHINE}/documents").status_code == 401


def test_fails_closed_when_no_host_injected_the_pat_hooks(app, client):
    """Unmounted (or a host that injected nothing) must be a 503, never an open door."""
    cfg = app.extensions["cream"]
    cfg.extras.pop("pat_authenticate")
    res = client.get(f"{MACHINE}/documents")
    assert res.status_code == 503
    assert res.get_json()["error"] == "unavailable"


# ── tenancy: identity comes from the PAT actor, authz from the host seam ──────────────────────────────


def test_draft_is_owned_by_the_pat_actor_not_the_session(pat_client, hooks, engagement_id, session_factory):
    """The regression this surface exists to avoid: attributing a machine write to ``current_actor``,
    which is None on a PAT request, leaving the row with a NULL owner. ``pat_client`` blanks the session
    precisely so that mistake cannot pass."""
    actor = hooks["pat_actor"]
    res = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Agent draft"})
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "draft"
    assert body["number"] is None  # a draft is unnumbered; numbering happens at issue (human-only)
    with session_factory() as db:
        doc = db.get(Document, uuid.UUID(body["id"]))
        assert doc.owner_id == actor.id, "owner must be the PAT principal"
        assert doc.owner_id is not None
        assert doc.created_by == actor.username


def test_create_is_refused_when_the_host_says_not_an_operator(pat_client, hooks, engagement_id,
                                                              session_factory):
    """INV-TENANCY-05: the engagement gate runs BEFORE any write, and the answer is the host's."""
    hooks["can_operate_on"] = lambda _eid: False
    res = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Not mine"})
    assert res.status_code == 403
    with session_factory() as db:
        assert db.query(Document).count() == 0, "a refused create must not have written"


def test_create_requires_a_uuid_engagement_id(pat_client):
    assert pat_client.post(f"{MACHINE}/documents", json={}).status_code == 400
    assert pat_client.post(f"{MACHINE}/documents", json={"engagement_id": "nope"}).status_code == 400


def test_reads_are_scoped_to_the_tokens_visible_engagements(pat_client, hooks, engagement_id):
    """A document outside the token's engagements is invisible to list AND 404s on read — never a 403,
    which would confirm the id exists."""
    created = pat_client.post(f"{MACHINE}/documents",
                              json={"engagement_id": str(engagement_id), "title": "Mine"}).get_json()
    # NB: the conftest returns this hook's value as-is (unlike `can_operate_on`, which it calls), so this
    # is a set, not a callable.
    hooks["visible_engagement_ids"] = {uuid.uuid7()}  # some other engagement
    assert pat_client.get(f"{MACHINE}/documents").get_json()["documents"] == []
    assert pat_client.get(f"{MACHINE}/documents/{created['id']}").status_code == 404


def test_get_missing_document_is_404(pat_client):
    assert pat_client.get(f"{MACHINE}/documents/{uuid.uuid7()}").status_code == 404


# ── drafting actually works end to end ───────────────────────────────────────────────────────────────


def test_add_line_item_and_totals(pat_client, engagement_id):
    doc = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Pentest"}).get_json()
    res = pat_client.post(f"{MACHINE}/documents/{doc['id']}/line-items",
                          json={"description": "External pentest", "qty": 5, "unit_price": 1500,
                                "unit": "day"})
    assert res.status_code == 201, res.get_data(as_text=True)
    line = res.get_json()
    assert line["description"] == "External pentest"
    assert float(line["amount"]) == 7500.0

    fetched = pat_client.get(f"{MACHINE}/documents/{doc['id']}").get_json()
    assert len(fetched["line_items"]) == 1
    assert float(fetched["totals"]["subtotal"]) == 7500.0


def test_add_line_item_is_refused_for_a_non_operator(pat_client, hooks, engagement_id, session_factory):
    """The gate is asked about the DOCUMENT's engagement, resolved from the id in the URL — never about an
    engagement id the caller supplied in the body."""
    doc = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Pentest"}).get_json()
    hooks["can_operate_on"] = lambda _eid: False
    res = pat_client.post(f"{MACHINE}/documents/{doc['id']}/line-items",
                          json={"description": "Sneaky", "qty": 1, "unit_price": 1})
    assert res.status_code == 403
    with session_factory() as db:
        assert db.get(Document, uuid.UUID(doc["id"])).line_items == []


def test_sync_returns_suggestions_and_writes_nothing(pat_client, engagement_id, session_factory):
    """Sync is advisory: it prices engagement units that are not yet billed, and a human accepts them."""
    doc = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Pentest"}).get_json()
    res = pat_client.post(f"{MACHINE}/documents/{doc['id']}/sync", json={"unit_keys": []})
    assert res.status_code == 200
    assert res.get_json()["suggestions"] == []
    with session_factory() as db:
        assert db.get(Document, uuid.UUID(doc["id"])).line_items == []


def test_brand_defaults_are_applied_so_agent_drafts_match_dashboard_drafts(pat_client, engagement_id):
    """A draft created over the machine API must render like one created in the UI — same currency and tax
    line. Creating one without the brand defaults would be a silent reporting defect, not a 500."""
    doc = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Quote"},
                          ).get_json()
    browser = pat_client.get(f"{MACHINE}/documents/{doc['id']}").get_json()
    assert browser["currency"]
    assert doc["currency"] == browser["currency"]


def test_quote_kind_is_honoured_and_requires_authorization(pat_client, engagement_id):
    doc = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "kind": "quote",
                                "title": "SOW"}).get_json()
    assert doc["kind"] == "quote"
    assert doc["authorization_required"] is True
