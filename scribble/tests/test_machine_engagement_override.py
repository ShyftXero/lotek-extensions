"""lotek#620: machine API PATCH /engagements/<id> — set/clear the manual overall-risk override.

Proves the write seam scribble OWNS: the coupled validation (a set override REQUIRES a non-empty
rationale), set/clear round-tripping through ``GET``/``_engagement_summary``, both override directions,
and that the route is gated by the ``write`` scope (a read-only token is refused). Auth/tenancy
pass-through is the ``stub_host``'s concern (proven in the lotek repo); this exercises scribble's logic.
"""
from __future__ import annotations

import uuid

from tests.conftest import StubActor, install_scope_enforcing_gate

M = "/scribble/machine"
ACME = uuid.uuid7()


def _make_engagement(client, stub_host, name: str = "Override E") -> str:
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    resp = client.post(f"{M}/engagements", json={"name": name, "client_id": ACME})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["id"]


def test_patch_sets_override_and_rationale(client, stub_host):
    eid = _make_engagement(client, stub_host)
    resp = client.patch(
        f"{M}/engagements/{eid}",
        json={"risk_override": "high", "risk_override_rationale": "Chained exposure raises real risk."},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["risk_override"] == "high"
    assert body["risk_override_rationale"] == "Chained exposure raises real risk."

    # persisted: a fresh GET echoes it
    got = client.get(f"{M}/engagements/{eid}").get_json()
    assert got["risk_override"] == "high"
    assert got["risk_override_rationale"] == "Chained exposure raises real risk."


def test_patch_override_without_rationale_is_400(client, stub_host):
    eid = _make_engagement(client, stub_host)
    resp = client.patch(f"{M}/engagements/{eid}", json={"risk_override": "low"})
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["error"] == "bad_request"
    # nothing was persisted
    got = client.get(f"{M}/engagements/{eid}").get_json()
    assert got["risk_override"] is None
    assert got["risk_override_rationale"] is None


def test_patch_blank_rationale_is_400(client, stub_host):
    eid = _make_engagement(client, stub_host)
    resp = client.patch(
        f"{M}/engagements/{eid}", json={"risk_override": "medium", "risk_override_rationale": "   "}
    )
    assert resp.status_code == 400, resp.get_json()


def test_patch_clear_nulls_both(client, stub_host):
    eid = _make_engagement(client, stub_host)
    client.patch(
        f"{M}/engagements/{eid}",
        json={"risk_override": "high", "risk_override_rationale": "reason"},
    )
    # explicit null clears the override; its rationale goes with it
    resp = client.patch(f"{M}/engagements/{eid}", json={"risk_override": None})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["risk_override"] is None
    assert body["risk_override_rationale"] is None


def test_patch_rejects_bad_band(client, stub_host):
    eid = _make_engagement(client, stub_host)
    resp = client.patch(
        f"{M}/engagements/{eid}",
        json={"risk_override": "catastrophic", "risk_override_rationale": "x"},
    )
    assert resp.status_code == 400, resp.get_json()


def test_patch_direction_both_ways(client, stub_host):
    # An override may move the band in either direction; both are accepted with a rationale.
    for band in ("critical", "info"):
        eid = _make_engagement(client, stub_host, name=f"dir-{band}")
        resp = client.patch(
            f"{M}/engagements/{eid}",
            json={"risk_override": band, "risk_override_rationale": f"adjusted to {band}"},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["risk_override"] == band


def test_patch_missing_engagement_is_404(client, stub_host):
    resp = client.patch(
        f"{M}/engagements/{uuid.uuid7()}",
        json={"risk_override": "high", "risk_override_rationale": "x"},
    )
    assert resp.status_code == 404, resp.get_json()


def test_patch_requires_write_scope(app, client, stub_host):
    # Create with the default (read+write) actor, then swap to a read-only token and prove the WRITE
    # scope decorator refuses the PATCH — the harness's no-op gate would otherwise hide a mis-scoped route.
    eid = _make_engagement(client, stub_host)
    install_scope_enforcing_gate(app, stub_host)
    stub_host.actor = StubActor(id=9, username="ro", role="operator", scopes=frozenset({"read"}))
    resp = client.patch(
        f"{M}/engagements/{eid}",
        json={"risk_override": "high", "risk_override_rationale": "should be refused"},
    )
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json()["error"] == "forbidden"
