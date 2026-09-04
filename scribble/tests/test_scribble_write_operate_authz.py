"""INV-TENANCY-05 on the PAT/MACHINE blueprint: a WRITE requires an OPERATOR capability on THAT
engagement, not merely client-coarse VIEW.

The bug this pins: every mutating machine route authorized only through `can_view_engagement` — which
resolves through the host's *client-coarse* `can_view_client`. A token holding an operator membership
under a client can therefore VIEW every engagement of that client, so it could also WRITE a *sibling*
engagement it holds no operator grant on, and an observer (view-only) membership could write at all.
INV-TENANCY-05 forbids both: the object gate must be the per-engagement `can_operate_on`, never
`can_view_client`/`can_write`.

The refusal is 403, not the module's usual existence-hiding 404, precisely BECAUSE the caller can view
the row (client-coarse) — see `test_operator_on_sibling_can_still_view_the_sibling`: a 404 would be a
lie, and 403 discloses nothing the read routes don't already show. This mirrors the cookie gate's
own two-axis split (view => 404, view-but-not-operate => 403) in `scribble.authz._gate`.

Red → green: with the `_deny_write(...)` guard removed from `api_pat.py`'s write routes, every 403
assertion below flips to 201/200 and the sibling/observer writes land — the exact
operator-on-E1-writes-E2 escalation the invariant names.
"""

from __future__ import annotations

import uuid

import scribble.models as fm
from tests.conftest import StubActor

M = "/scribble/machine"
CLIENT = uuid.uuid7()  # ONE client; two engagements live under it


def _engagement(session_factory):
    """A fresh engagement under the shared CLIENT -> (scribble_pk, core_engagement_id).

    The conftest `before_insert` listener stamps a distinct `core_engagement_id` on every directly-built
    engagement, so E1 and E2 differ on the exact key `can_operate_on` is asked about."""
    with session_factory() as db:
        eng = fm.Engagement(name="E", client_id=CLIENT)
        db.add(eng)
        db.commit()
        return eng.id, eng.core_engagement_id


def _finding_on(session_factory, engagement_id):
    with session_factory() as db:
        finding = fm.EngagementFinding(engagement_id=engagement_id, title="F", order_index=0)
        db.add(finding)
        db.commit()
        return finding.id


def _actor_viewing_client_operating(stub_host, *, core_engagement_ids):
    """A write-scoped operator that MAY VIEW the whole CLIENT (client-coarse) but only OPERATES the given
    core engagements — the exact shape of the vulnerability: view granted for the client, operate not."""
    stub_host.actor = StubActor(id=7, username="op", role="operator")
    stub_host.viewable_client_ids = {CLIENT}
    stub_host.operable_engagement_ids = set(core_engagement_ids)


def test_operator_on_sibling_cannot_write_the_other_engagement(client, stub_host, session_factory):
    e1, e1_core = _engagement(session_factory)
    e2, _e2_core = _engagement(session_factory)
    _actor_viewing_client_operating(stub_host, core_engagement_ids={e1_core})  # operator on E1 ONLY

    # Its OWN engagement is writable — the positive control that keeps the refusal below honest.
    ok = client.post(f"{M}/engagements/{e1}/groups", json={"name": "Section"})
    assert ok.status_code == 201, ok.get_json()

    # The sibling under the SAME client is view-visible but NOT operable -> refused, nothing written.
    denied = client.post(f"{M}/engagements/{e2}/groups", json={"name": "Section"})
    assert denied.status_code == 403, denied.get_json()
    with session_factory() as db:
        assert db.get(fm.Engagement, e2).groups == []


def test_observer_cannot_write_even_its_own_engagement(client, stub_host, session_factory):
    """An observer holds a membership (so it may VIEW the client) but operates nothing — indistinguishable
    from an operator under the old view-only gate, refused under the operate gate."""
    e1, _ = _engagement(session_factory)
    _actor_viewing_client_operating(stub_host, core_engagement_ids=set())  # view CLIENT, operate NOTHING

    denied = client.post(f"{M}/engagements/{e1}/groups", json={"name": "Section"})
    assert denied.status_code == 403, denied.get_json()
    with session_factory() as db:
        assert db.get(fm.Engagement, e1).groups == []


def test_operator_on_sibling_can_still_view_the_sibling(client, stub_host, session_factory):
    """Why the write refusal is 403, not 404: the caller genuinely CAN read E2 (client-coarse), so the
    existence-oracle argument that makes every other refusal here a 404 does not apply — 403 leaks nothing
    a plain GET does not already reveal."""
    e1, e1_core = _engagement(session_factory)
    e2, _ = _engagement(session_factory)
    _actor_viewing_client_operating(stub_host, core_engagement_ids={e1_core})

    assert client.get(f"{M}/engagements/{e2}").status_code == 200


def test_operate_gate_holds_across_the_resolution_paths(client, stub_host, session_factory):
    """Blueprint-wide, not one route: exercise a write on the sibling through each of the three ways a
    machine route resolves its engagement — `_resolve_engagement` (engagement id in URL),
    `_visible_engagement` (engagement id in URL, PK-only), and `_visible_finding` (child id -> engagement).
    Every one must 403; none may mutate."""
    e1, e1_core = _engagement(session_factory)
    e2, _ = _engagement(session_factory)
    _actor_viewing_client_operating(stub_host, core_engagement_ids={e1_core})

    tmpl_id = _template(session_factory)
    f2 = _finding_on(session_factory, e2)  # a finding that lives on the sibling

    writes = [
        # _resolve_engagement path (engagement id in URL)
        ("POST", f"{M}/engagements/{e2}/findings", {"template_id": tmpl_id}),
        ("PATCH", f"{M}/engagements/{e2}", {"risk_override": "high", "risk_override_rationale": "x"}),
        ("POST", f"{M}/engagements/{e2}/artifacts",
         {"filename": "e.txt", "content_base64": "YWJj"}),
        # _visible_engagement path
        ("POST", f"{M}/engagements/{e2}/groups", {"name": "S"}),
        # _visible_finding path (child id resolves to the sibling engagement)
        ("POST", f"{M}/findings/{f2}/move", {"group_id": None}),
        ("PATCH", f"{M}/findings/{f2}", {"title": "changed"}),
        ("DELETE", f"{M}/findings/{f2}", {}),
    ]
    landed = []
    for method, url, body in writes:
        resp = client.open(url, method=method, json=body)
        if resp.status_code != 403:
            landed.append((method, url, resp.status_code, resp.get_json()))
    assert landed == [], f"sibling-engagement write(s) were NOT refused 403: {landed}"

    with session_factory() as db:
        e2_row = db.get(fm.Engagement, e2)
        assert e2_row.findings and e2_row.findings[0].title == "F"  # untouched
        assert e2_row.groups == []
        assert e2_row.risk_override is None


def _template(session_factory):
    with session_factory() as db:
        tmpl = fm.VulnerabilityTemplate(name="T", content_json={}, content_html={})
        db.add(tmpl)
        db.commit()
        return tmpl.id
