"""The published machine-API contract — ``GET /scribble/machine/openapi.json`` (#116).

The bug this guards against is not a crash. A client written against a guessed response key gets a
``KeyError`` at best and, worse, a verification script that guesses the wrong LIST key reports **zero
results for data that is actually present** — which is what happened on the engagement that filed #116.
So the assertions here are about the DOCUMENT describing the payload the routes really return, and about
a new route being unable to ship undocumented.
"""

from __future__ import annotations

import pytest

from scribble import openapi

M = "/scribble/machine"


@pytest.fixture
def spec(client, stub_host):  # noqa: ARG001 — stub_host wires the PAT seam this route needs
    resp = client.get(f"{M}/openapi.json")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_openapi_document_is_served(spec):
    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["title"] == "Scribble machine API"
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"


def test_every_machine_route_is_documented_with_a_response_schema(app, spec):
    """The drift guard: a route added to the machine blueprint with no ``_RESPONSES`` entry fails HERE,
    at the cheap end, rather than by a client discovering the gap against a live deliverable."""
    undocumented = [
        view.__name__
        for _rule, view in openapi.machine_views(app, "scribble_machine")
        if view.__name__ not in openapi._RESPONSES
    ]
    assert undocumented == [], (
        f"machine routes with no declared response schema: {undocumented}. "
        "Add them to scribble/openapi.py::_RESPONSES."
    )

    for path, item in spec["paths"].items():
        for method, op in item.items():
            success = [code for code in op["responses"] if code.startswith("2")]
            assert success, f"{method.upper()} {path} documents no success response"
            body = op["responses"][success[0]]["content"]["application/json"]["schema"]
            assert body, f"{method.upper()} {path} success response has an empty schema"
            assert op["x-required-scope"] in {"read", "write"}


def test_documented_paths_cover_the_attack_path_item_routes(spec):
    """#114's new per-item routes are reachable from the document alone."""
    item = "/scribble/machine/engagements/{engagement_id}/attack-paths/{attack_path_id}"
    assert set(spec["paths"][item]) == {"get", "patch", "delete"}
    assert spec["paths"][item]["delete"]["x-required-scope"] == "write"
    assert spec["paths"][item]["get"]["x-required-scope"] == "read"


def test_collection_keys_in_the_document_match_what_the_routes_return(client, stub_host, spec):
    """The document is only worth anything if it agrees with the wire. Drive the two collections #116
    named and assert the documented key names are the ones actually present."""
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {901}
    eid = client.post(f"{M}/engagements", json={"name": "E", "client_id": 901}).get_json()["id"]
    client.post(f"{M}/engagements/{eid}/attack-paths", json={"embed_html": "<html></html>"})
    client.post(f"{M}/engagements/{eid}/findings", json={"title": "T", "severity": "high"})

    listing = client.get(f"{M}/engagements/{eid}/attack-paths").get_json()
    documented = spec["paths"]["/scribble/machine/engagements/{engagement_id}/attack-paths"]["get"]
    schema = documented["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) <= set(listing), (
        "the document promises keys the route does not return"
    )
    assert "attack_paths" in listing and "diagrams" in listing

    findings = client.get(f"{M}/engagements/{eid}/findings").get_json()
    f_schema = spec["paths"]["/scribble/machine/engagements/{engagement_id}/findings"]["get"][
        "responses"]["200"]["content"]["application/json"]["schema"]
    assert set(f_schema["properties"]) <= set(findings)
    # The document says findings are nested, not flat. Pin that it is telling the truth.
    assert "findings" not in findings
    assert findings["groups"] or findings["ungrouped"]


def test_request_bodies_are_hoisted_into_components(spec):
    """A client needs the WRITE shape too — the pydantic request models must resolve, not dangle."""
    post_engagements = spec["paths"]["/scribble/machine/engagements"]["post"]
    ref = post_engagements["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    name = ref.rsplit("/", 1)[-1]
    assert name in spec["components"]["schemas"]
    assert "name" in spec["components"]["schemas"][name]["properties"]


def test_finding_id_is_a_read_alias_for_id(client, stub_host):
    """#116 fix 3: the write side accepts ``finding_id``, so the read side must emit it too."""
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {902}
    eid = client.post(f"{M}/engagements", json={"name": "E", "client_id": 902}).get_json()["id"]
    created = client.post(
        f"{M}/engagements/{eid}/findings", json={"title": "SQLi", "severity": "high"}
    ).get_json()

    listing = client.get(f"{M}/engagements/{eid}/findings").get_json()
    rows = [f for g in listing["groups"] for f in g["findings"]] + listing["ungrouped"]
    assert rows, listing
    for row in rows:
        assert row["finding_id"] == row["id"]

    detail = client.get(f"{M}/findings/{created['finding_id']}").get_json()
    assert detail["finding_id"] == detail["id"] == created["finding_id"]
