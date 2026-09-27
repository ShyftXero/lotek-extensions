"""The published machine-API contract — ``GET /scribble/machine/openapi.json`` (#116).

The bug this guards against is not a crash. A client written against a guessed response key gets a
``KeyError`` at best and, worse, a verification script that guesses the wrong LIST key reports **zero
results for data that is actually present** — which is what happened on the engagement that filed #116.
So the assertions here are about the DOCUMENT describing the payload the routes really return, and about
a new route being unable to ship undocumented.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from scribble import openapi
from scribble.api_schemas import IDEMPOTENT_ATTR, REQUEST_MODEL_ATTR

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
    at the cheap end, rather than by a client discovering the gap against a live deliverable.

    Two assertions, and they are NOT redundant. The first reaches into the private ``_RESPONSES`` table —
    that is the white-box check, and it is the one that names the missing route. The second walks the
    PUBLISHED document and refuses the *fallback* schema by its description: without that, the loop is
    decorative, because ``build_spec``'s fallback is a non-empty dict and a bare ``assert body`` can never
    be false. So if the white-box half is ever refactored away, the black-box half still catches an
    "Undocumented" placeholder reaching the real contract.
    """
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
            content = op["responses"][success[0]]["content"]
            body = next(iter(content.values()))["schema"]
            assert body, f"{method.upper()} {path} success response has an empty schema"
            assert "Undocumented" not in str(body.get("description", "")), (
                f"{method.upper()} {path} published the placeholder schema — the real one is missing"
            )
            assert op["x-required-scope"] in {"read", "write"}


# Machine routes that take a JSON body but declare NO ``@request_body`` model. Every entry is a
# DOCUMENTED gap in the published contract, not an exemption on the merits — a client still has to read
# the source to learn these bodies. The set exists so the guard below can be a ratchet: it is seeded with
# what already shipped that way, and a NEW route with an undeclared body fails immediately.
#
# It caught its own author: `PATCH …/attack-paths/<id>` landed on this branch with no model, so the very
# PR that published the document shipped a route whose body it did not describe. Shrink this set, never
# grow it.
_UNDECLARED_REQUEST_BODIES = frozenset(
    {
        "scribble_create_vuln_map",
        "scribble_resolve_template",
        # `scribble_promote_job` was here until lotek#656 gave it a body worth declaring
        # (`acknowledge_inconclusive`). The positive control below is what makes removing it MANDATORY
        # rather than optional once the model exists.
        "scribble_delete_finding",
        "scribble_delete_group",
        "scribble_delete_attack_path",
    }
)


def test_every_body_taking_route_declares_its_request_shape(app, spec):
    """The request half of the same drift property.

    ``openapi.py`` introspects paths, parameters and scopes from the live ``url_map``, so a route cannot
    be ABSENT from the document — but its request body is only described if someone stamped
    ``@request_body(Model)``, exactly as the response is only described if someone added a ``_RESPONSES``
    entry. The response side had a guard from day one and the request side did not, which is how this
    branch's own PATCH route reached review with a documented response and an undocumented body.
    """
    missing = [
        view.__name__
        for rule, view in openapi.machine_views(app, "scribble_machine")
        if (rule.methods or set()) & {"POST", "PUT", "PATCH"}
        and getattr(view, REQUEST_MODEL_ATTR, None) is None
        and view.__name__ not in _UNDECLARED_REQUEST_BODIES
    ]
    assert missing == [], (
        f"machine route(s) accepting a body with no declared request model: {missing}. Add a model to "
        "scribble/api_schemas.py and stamp it with @request_body, or — only if the route genuinely takes "
        "no meaningful body — add it to _UNDECLARED_REQUEST_BODIES with a reason."
    )

    # Positive control: the set above must not be able to hide a route that DOES declare one, or the
    # ratchet could be loosened by padding it rather than by fixing the route.
    for name in _UNDECLARED_REQUEST_BODIES:
        view = next(
            (v for _r, v in openapi.machine_views(app, "scribble_machine") if v.__name__ == name), None
        )
        assert view is not None, f"{name} is listed as undeclared but is not a machine route any more"
        assert getattr(view, REQUEST_MODEL_ATTR, None) is None, (
            f"{name} DOES declare a request model — remove it from _UNDECLARED_REQUEST_BODIES"
        )

    # And the document really carries the declared body for the route this branch added.
    patch_op = spec["paths"][
        "/scribble/machine/engagements/{engagement_id}/attack-paths/{attack_path_id}"
    ]["patch"]
    ref = patch_op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    model = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    assert set(model["properties"]) >= {"include_in_report", "caption"}


def test_refusals_a_client_must_handle_are_documented(spec):
    """A document a client can be written from has to name the refusals it will actually meet.

    `400` and `409` were both missing until review caught it: every mutating route can 400 on a
    malformed body (this branch's own tests assert two such cases), and `409` — the host seam's "the
    original request under this key is still running" — became reachable on this blueprint FOR THE FIRST
    TIME with the idempotency fix. A generated client that does not know `409` reads it as unknown and
    retries harder, which is the worst possible response to it.
    """
    for path, item in spec["paths"].items():
        for method, op in item.items():
            codes = set(op["responses"])
            assert {"401", "403", "404", "503"} <= codes, (method, path, sorted(codes))
            if method in ("post", "put", "patch", "delete"):
                assert "400" in codes, (method, path, sorted(codes))


def test_the_retry_refusals_are_documented_where_and_only_where_they_can_happen(app, spec):
    """An operation must advertise the idempotency seam's refusals **iff** it actually routes through it.

    Keying them off the HTTP method — which is what this generator did until review caught it — promised
    retry-safety on four routes that have none, `POST …/promote-job/{job_id}` among them, and that one
    BULK-CREATES findings. A client retries on the promise, so a false one is worse than silence. This
    asserts the document against the `IDEMPOTENT_ATTR` stamp in both directions.

    🔴 The assertion is on `x-idempotency-refusal`, NOT on the presence of `409`. Those were the same
    statement only while the seam was the sole thing on this blueprint that could answer 409; `promote-job`
    now refuses `job_not_assessed` (lotek#656) and has refused `cross_engagement` since lotek#845, both
    with a 409 of their own. Reading a bare `409` as "promises retry-safety" would force the choice
    between publishing a false promise and hiding two real refusals from every generated client. `422`
    still keys off the bare code, because it has no source other than the seam.
    """
    by_op = {
        rule.endpoint: bool(getattr(view, IDEMPOTENT_ATTR, False))
        for rule, view in openapi.machine_views(app, "scribble_machine")
    }
    seen_true = seen_false = seen_unseamed_409 = False
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method not in ("post", "put", "patch", "delete"):
                continue
            honoured = by_op[op["operationId"]]
            codes = set(op["responses"])
            conflict = op["responses"].get("409")
            if honoured:
                seen_true = True
                assert conflict is not None and "422" in codes, (
                    f"{method.upper()} {path} routes through the idempotency seam but does not document "
                    f"its refusals: {sorted(codes)}"
                )
                assert conflict["x-idempotency-refusal"] is True, (method, path)
                # The seam really answers `error: "conflict"` (lotek `app/idempotency.py`), so that code
                # has to be among the ones the document tells a client to expect.
                assert "conflict" in conflict["x-refusal-codes"], (method, path, conflict)
            else:
                seen_false = True
                assert "422" not in codes, (
                    f"{method.upper()} {path} advertises retry-safety it does NOT have: {sorted(codes)}"
                )
                if conflict is not None:
                    seen_unseamed_409 = True
                    assert conflict["x-idempotency-refusal"] is False, (
                        f"{method.upper()} {path} does not route through the seam but its 409 claims to "
                        "be the seam's — that is the false retry-safety promise, one level down."
                    )
                    assert conflict["x-refusal-codes"], (method, path)
    # All three arms must have run, or this passes vacuously the day the stamp is applied (or dropped)
    # wholesale — `seen_unseamed_409` specifically, or the new branch above is never exercised at all.
    assert seen_true and seen_false and seen_unseamed_409, (seen_true, seen_false, seen_unseamed_409)


def test_promote_job_publishes_its_own_refusals_not_the_seam_s(spec):
    """`POST …/promote-job/{job_id}` is the route the two properties above pull in opposite directions on.

    It BULK-CREATES findings and does not route through the idempotency seam, so it must not carry `422`
    or claim the seam's 409. It also owns two real 409s a client has to be able to branch on:
    `job_not_assessed` (lotek#656 — the scan coverage will not support a deliverable) and
    `cross_engagement` (lotek#845). Both are terminal for the call as sent: retrying the identical request
    refuses identically, which is exactly what an undocumented 409 provokes a client into doing.
    """
    op = spec["paths"]["/scribble/machine/engagements/{engagement_id}/promote-job/{job_id}"]["post"]
    conflict = op["responses"]["409"]
    assert conflict["x-idempotency-refusal"] is False
    assert conflict["x-refusal-codes"] == ["cross_engagement", "job_not_assessed"]
    assert "422" not in op["responses"]
    assert "acknowledge_inconclusive" in conflict["description"]

    # The override is a documented request field, and the body is OPTIONAL — the ordinary promotion sends
    # none at all, so a generated client must not be built to demand one.
    body = op["requestBody"]
    assert body["required"] is False, "a bodyless promote is the normal call"
    model = spec["components"]["schemas"][
        body["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    ]
    assert "acknowledge_inconclusive" in model["properties"]
    assert not model.get("required"), "nothing in this body is mandatory"

    # And the success payload carries the verdict, not just the counters: a caller that never sends the
    # flag still has to be able to learn what coverage its findings were promoted under.
    ok = op["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert {"coverage_acknowledged", "assessed", "unassessed_modules"} <= set(ok)
    assert ok["assessed"]["type"] == ["boolean", "null"], "null is a verdict, not a missing value"


def test_the_report_route_is_not_documented_as_json(spec):
    """It streams HTML or a .docx; a client that JSON-decodes it fails in exactly the silent way #116 is
    about, only in the other direction."""
    content = spec["paths"]["/scribble/machine/engagements/{engagement_id}/report"]["get"][
        "responses"]["200"]["content"]
    assert "application/json" not in content
    assert "text/html" in content


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


# ── the audit half of the same "declared == real" property (INV-AUDIT-03) ────────────────────────────


def _emitted_audit_verbs() -> set[str]:
    """Every bare verb ANY scribble module passes to ``_audit``, read out of the source.

    Source text, not runtime: a verb is only emitted when its route actually runs, so a runtime probe
    would need every route driven to completion and would silently under-report the ones it missed —
    which is the failure mode this guard exists to prevent.

    Scans the whole package rather than a named file, and that widening IS the fix for a real blind
    spot: this guard used to read `api_pat.py` alone. When `themes_api.py` arrived emitting four new
    verbs, the declared-vs-emitted property silently stopped holding and this test stayed GREEN,
    because the second file was invisible to it. A guard that only watches the place the problem was
    first found is a guard that expires the moment the code grows — so it now watches every module
    that can emit, and a NEW module emitting a new verb is covered without anyone remembering to add
    it here.
    """
    pkg = Path(__file__).resolve().parents[1] / "scribble"
    pattern = re.compile(r'_audit\(\s*\w+,\s*"([a-z_]+)"')
    verbs: set[str] = set()
    scanned = 0
    for path in sorted(pkg.rglob("*.py")):
        text = path.read_text()
        if "_audit(" not in text:
            continue
        scanned += 1
        verbs.update(pattern.findall(text))
    assert scanned, "no module in the package calls _audit — the scan has drifted from the source"
    return verbs


def _declared_audit_verbs() -> set[str]:
    manifest = Path(__file__).resolve().parents[1] / "lotek-extension.toml"
    return set(tomllib.loads(manifest.read_text()).get("audit", {}).get("verbs", []))


def test_every_emitted_audit_verb_is_registered_in_the_manifest():
    """INV-AUDIT-03 (lotek core, **active**): an extension's audit action must be drawn from a registered
    vocabulary that the reader's action filter includes.

    The host namespaces each manifest verb to ``ext:scribble:<verb>`` and merges it into `/admin/audit`'s
    action filter (`app.extensions.extension_audit_actions`). A verb this module EMITS but does not
    DECLARE is therefore written to the audit table and then unselectable in the reader — the row exists
    and the question "who deleted the attack path out of the client's deliverable" still has no answer.
    That is the exact gap the vocabulary was introduced to close, and it reopened silently on this branch
    (`update_attack_path` / `delete_attack_path` were emitted for a full review cycle before this guard
    existed), because nothing compared the two lists.
    """
    emitted, declared = _emitted_audit_verbs(), _declared_audit_verbs()
    assert emitted, "found no _audit call sites at all — the regex above has drifted from the source"
    assert emitted - declared == set(), (
        f"audit verb(s) emitted by the package but missing from lotek-extension.toml [audit] verbs: "
        f"{sorted(emitted - declared)} — they will be invisible in /admin/audit's action filter."
    )


def test_no_declared_audit_verb_is_a_false_promise():
    """The other direction, and it is not symmetry for its own sake: a verb in the dropdown that nothing
    emits tells an operator a filter exists for an event this extension never records, so an empty result
    reads as "it didn't happen" rather than "nothing can report it"."""
    emitted, declared = _emitted_audit_verbs(), _declared_audit_verbs()
    # `report_read` is emitted by report_html_api.py, not api_pat.py — the only verb outside this module.
    extra = declared - emitted - {"report_read"}
    assert extra == set(), (
        f"audit verb(s) declared in lotek-extension.toml that api_pat.py never emits: {sorted(extra)}"
    )


# Request-model id fields that are HOST (core) references, not scribble rows. A SoftHostId is an int on
# a legacy/standalone host and a UUID under lotek v2, so `int | str` is the honest declaration and
# narrowing it to UUID would refuse a legitimate caller. Everything NOT in here is a scribble-owned PK
# and must be uuid-typed.
_SOFT_HOST_ID_FIELDS = frozenset({"client_id", "core_engagement_id", "lotek_finding_id"})


def test_request_model_ids_are_uuid_typed(spec):
    """The request half of #116, and the half that had no guard at all until review found it.

    `components.schemas` is generated from the pydantic models, so their declared types ARE the published
    contract — and they went stale at the UUIDv7 migration (#36 / lotek#335), still declaring `integer`
    for eight fields the handlers parse with `_opt_uuid`. A client generated from the document sent
    `{"template_id": 1}` and got a 400 on every call: the exact silent-wrong-guess failure this issue was
    filed about, published with authority.

    Walks the DOCUMENT rather than the classes, deliberately — what a client reads is what must be right.
    """
    offenders = []
    for model_name, schema in spec["components"]["schemas"].items():
        for field, prop in (schema.get("properties") or {}).items():
            if not (field.endswith("_id") or field.endswith("_ids")) or field in _SOFT_HOST_ID_FIELDS:
                continue
            # A nullable field is rendered `anyOf: [...]`; an array as `items`. Flatten and look for any
            # integer branch, which is the failure — not for a uuid branch, which a `$ref` could hide.
            blobs = [prop, *(prop.get("anyOf") or []), *([prop["items"]] if "items" in prop else [])]
            blobs += [b for blob in blobs for b in (blob.get("anyOf") or [])]
            blobs += [blob["items"] for blob in blobs if isinstance(blob.get("items"), dict)]
            if any(b.get("type") == "integer" for b in blobs):
                offenders.append(f"{model_name}.{field}")
    assert offenders == [], (
        f"request-model id field(s) published as `integer` on a UUID-keyed API: {sorted(set(offenders))}. "
        "Retype them `uuid.UUID` in scribble/api_schemas.py, or — only if the field is a core "
        "SoftHostId — add it to _SOFT_HOST_ID_FIELDS here with a reason."
    )


def test_soft_host_id_fields_stay_permissive(spec):
    """The positive control for the allowlist above: a SoftHostId must keep accepting BOTH shapes, so the
    exemption cannot be used to hide a field that has simply been left as an int."""
    props = spec["components"]["schemas"]["CreateEngagementRequest"]["properties"]
    for field in ("client_id", "core_engagement_id"):
        branches = {b.get("type") for b in props[field].get("anyOf", [])}
        assert {"integer", "string"} <= branches, (field, branches)
