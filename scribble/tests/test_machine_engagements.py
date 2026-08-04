"""Machine (PAT/Bearer) API — engagement create + template list/get + add-finding —
`scribble/api_pat.py` mounted at `/scribble/machine/*`.

Ported from the deleted lotek `tests/test_api_v1_scribble.py` (the routes moved off
`/api/v1/scribble/*` in the reporting-decoupling refactor, CONTRACT.md §4). Auth/scope RBAC is the
HOST's own concern (proven against a REAL lotek host in the lotek repo's
`tests/test_extension_machine_prefix.py`); this file proves scribble's OWN logic against the
`stub_host` fixture: tenancy pass-through, create/list/add-finding behavior, and — the two
HIGHEST-VALUE assertions per CONTRACT-FACTS §7 — that a missing job and an unauthorized job are
INDISTINGUISHABLE to the extension end-to-end through the machine route.
"""

from __future__ import annotations

import scribble.models as fm
from tests.conftest import FakeFindingDTO, StubActor

M = "/scribble/machine"


# ── fail-closed: no mounting host at all ─────────────────────────────────────────────────────────


def test_machine_route_503_with_no_host_mounted(client):
    """Standalone Scribble (no `stub_host` wired) has no PAT scheme -- a machine route must refuse,
    never run unauthenticated (`scribble/host.py::_no_host`)."""
    resp = client.get(f"{M}/templates")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "unavailable"


# ── create engagement ─────────────────────────────────────────────────────────────────────────────


def test_create_engagement_sets_created_by_and_owner_id(client, stub_host, session_factory):
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    resp = client.post(
        f"{M}/engagements",
        json={"name": "Acme external", "scope_type": "external", "company_name": "Acme"},
    )
    assert resp.status_code == 201
    eid = resp.get_json()["id"]
    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert eng is not None
        assert eng.created_by == "opA"
        assert eng.owner_id == 7
        assert eng.company_name == "Acme"


def test_create_engagement_requires_name(client, stub_host):
    resp = client.post(f"{M}/engagements", json={})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "bad_request"


def test_create_engagement_rejects_non_int_client_id(client, stub_host):
    resp = client.post(f"{M}/engagements", json={"name": "E", "client_id": "not-an-int"})
    assert resp.status_code == 400


def test_create_engagement_repoints_client_id_to_injected_host_client(app, session_factory):
    """The `client_id` a machine caller passes is a soft reference resolved through whatever
    `client_model` the host injected at mount time -- proving the machine route (not just the
    browser `engagement_new` route already covered by `test_client_model_injection.py`) also flows
    through a repointed host client model."""
    from tests.conftest import StubHost, _wire_stub_host

    cfg = app.extensions["scribble"]
    stub = StubHost()
    _wire_stub_host(cfg, stub)

    with session_factory() as db:
        host_client = fm.Client(name="Acme Corp")  # stand-in "host" row on scribble's own table
        db.add(host_client)
        db.commit()
        cid = host_client.id

    resp = app.test_client().post(f"{M}/engagements", json={"name": "Acme external", "client_id": cid})
    assert resp.status_code == 201
    eid = resp.get_json()["id"]
    with app.app_context(), session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        resolved = eng.resolve_client(db)
        assert resolved is not None and resolved.id == cid


# ── templates ─────────────────────────────────────────────────────────────────────────────────────


def test_list_templates_returns_seeded_library(client, stub_host):
    resp = client.get(f"{M}/templates")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] > 0
    t = body["items"][0]
    assert {"id", "name", "default_severity"} <= set(t)


def test_get_template_and_404(client, stub_host):
    tid = client.get(f"{M}/templates").get_json()["items"][0]["id"]
    assert client.get(f"{M}/templates/{tid}").status_code == 200
    assert client.get(f"{M}/templates/999999").status_code == 404


# ── add-finding: from a library template ─────────────────────────────────────────────────────────


def test_add_finding_from_template(client, stub_host, session_factory):
    eid = client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]
    tid = client.get(f"{M}/templates").get_json()["items"][0]["id"]
    resp = client.post(
        f"{M}/engagements/{eid}/findings",
        json={"template_id": tid, "target_host": "10.0.0.5"},
    )
    assert resp.status_code == 201
    fid = resp.get_json()["finding_id"]
    with session_factory() as db:
        f = db.get(fm.EngagementFinding, fid)
        assert f is not None and f.engagement_id == eid and f.template_id == tid
        assert f.target_host == "10.0.0.5" and f.order_index == 0


def test_add_finding_requires_a_source(client, stub_host):
    eid = client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]
    resp = client.post(f"{M}/engagements/{eid}/findings", json={})
    assert resp.status_code == 400


def test_add_finding_rejects_deactivated_template(client, stub_host, session_factory):
    eid = client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]
    tid = client.get(f"{M}/templates").get_json()["items"][0]["id"]
    with session_factory() as db:
        db.get(fm.VulnerabilityTemplate, tid).active = False
        db.commit()
    resp = client.post(f"{M}/engagements/{eid}/findings", json={"template_id": tid})
    assert resp.status_code == 404


def test_add_finding_rejects_non_integer_ids(client, stub_host):
    eid = client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]
    for bad in ({"template_id": []}, {"lotek_finding_id": {}}, {"group_id": [1], "template_id": 1}):
        resp = client.post(f"{M}/engagements/{eid}/findings", json=bad)
        assert resp.status_code == 400, bad


def test_add_finding_engagement_not_found(client, stub_host):
    resp = client.post(f"{M}/engagements/999999/findings", json={"template_id": 1})
    assert resp.status_code == 404


# ── HIGHEST-VALUE: promoting a lotek scan finding is tenancy-checked end-to-end ────────────────────


def test_promote_lotek_finding_respects_job_tenancy(client, stub_host, session_factory):
    """A missing job and an unauthorized job must be INDISTINGUISHABLE to the extension: the tenancy
    decision is entirely the HOST's (`host.findings().get_finding`), and `scribble/api_pat.py` never
    sees, and cannot widen, that decision -- it only ever sees `None` and 404s."""
    dto = FakeFindingDTO(id=101, title="RCE via upload", source="autorecon")
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[dto])

    def _promote(actor):
        stub_host.actor = actor
        eid = client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]
        return client.post(f"{M}/engagements/{eid}/findings", json={"lotek_finding_id": 101})

    # opB (id=8) does not own the finding's job -> refused as 404 (no existence leak)
    assert _promote(StubActor(id=8, username="opB", role="operator")).status_code == 404
    # owner opA (id=7) and admin -> allowed
    r_a = _promote(StubActor(id=7, username="opA", role="operator"))
    assert r_a.status_code == 201
    assert _promote(StubActor(id=1, username="admin", role="admin")).status_code == 201
    with session_factory() as db:
        f = db.get(fm.EngagementFinding, r_a.get_json()["finding_id"])
        assert f.title == "RCE via upload"  # from_lotek_finding bridge, verbatim title


def test_promote_finding_with_missing_job_fails_closed(client, stub_host):
    """A `lotek_finding_id` that resolves to no job at all (dangling / never registered with the stub)
    must NOT be promotable by anyone, including an admin -- fail CLOSED, not skip the check because
    there's no job to check against. Identical 404 shape to the unauthorized case above."""
    stub_host.actor = StubActor(id=1, username="admin", role="admin")  # even admin can't promote a dangler
    eid = client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]
    resp = client.post(f"{M}/engagements/{eid}/findings", json={"lotek_finding_id": 999999})
    assert resp.status_code == 404
    assert resp.get_json()["detail"] == "lotek finding not found"


def test_promote_lotek_finding_dedups_on_source_finding_id(client, stub_host, session_factory):
    """Re-promoting the SAME lotek finding into the SAME engagement returns the existing authored
    finding (200, deduped) instead of duplicating it."""
    dto = FakeFindingDTO(id=202, title="RCE via upload")
    stub_host.findings.add_job("job-2", owner_id=7, dtos=[dto])
    stub_host.actor = StubActor(id=7, username="opA", role="operator")
    eid = client.post(f"{M}/engagements", json={"name": "E"}).get_json()["id"]

    r1 = client.post(f"{M}/engagements/{eid}/findings", json={"lotek_finding_id": 202})
    assert r1.status_code == 201
    r2 = client.post(f"{M}/engagements/{eid}/findings", json={"lotek_finding_id": 202})
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["deduped"] is True
    assert body["finding_id"] == r1.get_json()["finding_id"]
    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert len([f for f in eng.findings if f.source_finding_id == 202]) == 1
