"""The PAT machine surface — how an AGENT files a bug report.

Cross-user refusals live in ``test_authz.py``; this file covers the CRUD contract itself and the two
things a stub host makes easy to get silently wrong: the declared **scope** on each route, and the
extension refusing outright when the host injected **no** PAT capabilities at all.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import StubActor, load, loaded

from bugreport.models import ReportStatus

URL = "/bugreport/machine/reports"


def test_create_list_get_patch_delete(pat_client, hooks):
    created = pat_client.post(URL, json={"title": "agent found a wedge", "body": "poll loop stalls"})
    assert created.status_code == 201
    report = created.get_json()["report"]
    rid = report["id"]
    # Attribution comes from the TOKEN, never from the body — there is no reporter field to spoof.
    assert report["reporter_id"] == str(hooks["pat_actor"].id)
    assert report["reporter_name"] == "agent"
    assert report["status"] == "open"

    listed = pat_client.get(URL).get_json()["reports"]
    assert [r["id"] for r in listed] == [rid]

    fetched = pat_client.get(f"{URL}/{rid}").get_json()["report"]
    assert fetched["title"] == "agent found a wedge"

    patched = pat_client.patch(f"{URL}/{rid}", json={"body": "poll loop stalls after 3 cycles"})
    assert patched.status_code == 200
    assert patched.get_json()["report"]["body"] == "poll loop stalls after 3 cycles"
    # A partial edit must not blank the field it did not send.
    assert patched.get_json()["report"]["title"] == "agent found a wedge"

    assert pat_client.delete(f"{URL}/{rid}").status_code == 200
    assert load(pat_client, rid) is None


def test_a_reporter_edit_and_an_admin_response_cannot_share_one_call(pat_client, hooks):
    hooks["pat_actor"] = StubActor(username="root", role="admin")
    rid = pat_client.post(URL, json={"title": "mixed", "body": ""}).get_json()["report"]["id"]
    resp = pat_client.patch(f"{URL}/{rid}", json={"title": "edited", "status": "resolved"})
    assert resp.status_code == 400
    row = loaded(pat_client, rid)
    assert row.title == "mixed" and row.status is ReportStatus.open


def test_an_empty_patch_is_a_400(pat_client):
    rid = pat_client.post(URL, json={"title": "nothing to do", "body": ""}).get_json()["report"]["id"]
    assert pat_client.patch(f"{URL}/{rid}", json={}).status_code == 400


def test_validation_is_enforced_on_the_machine_surface_too(pat_client):
    assert pat_client.post(URL, json={"title": "", "body": "x"}).status_code == 400
    assert pat_client.post(URL, json={"title": "a" * 500, "body": ""}).status_code == 400


def test_a_missing_report_is_404_on_every_route(pat_client):
    missing = uuid.uuid7()
    assert pat_client.get(f"{URL}/{missing}").status_code == 404
    assert pat_client.patch(f"{URL}/{missing}", json={"title": "x"}).status_code == 404
    assert pat_client.delete(f"{URL}/{missing}").status_code == 404


def test_a_read_only_token_cannot_write(pat_client, hooks):
    """Proves each route DECLARES the right scope: the fixture's `require_pat_scope` really enforces it,
    so a missing/incorrect decorator shows up here rather than passing against a no-op stub."""
    hooks["pat_actor"] = StubActor(scopes=frozenset({"read"}))
    assert pat_client.get(URL).status_code == 200
    assert pat_client.post(URL, json={"title": "x", "body": ""}).status_code == 403
    assert pat_client.patch(f"{URL}/{uuid.uuid7()}", json={"title": "x"}).status_code == 403
    assert pat_client.delete(f"{URL}/{uuid.uuid7()}").status_code == 403


def test_no_token_is_401(pat_client, hooks):
    hooks["pat_actor"] = None
    assert pat_client.get(URL).status_code == 401
    assert pat_client.post(URL, json={"title": "x", "body": ""}).status_code == 401


def test_a_rejected_token_never_reaches_a_route(pat_client, hooks):
    """`pat_authenticate` is a blueprint `before_request`: when the host rejects the token, its response
    is returned and no view runs."""
    hooks["pat_authenticate"] = ({"error": "unauthorized"}, 401)
    assert pat_client.get(URL).status_code == 401
    assert pat_client.post(URL, json={"title": "should not exist", "body": ""}).status_code == 401


def test_with_no_host_pat_capabilities_the_machine_api_refuses(standalone_app):
    """Standalone has no PAT scheme at all. The machine blueprint must 503, never serve unauthenticated —
    ``bugreport/host.py`` fails CLOSED when the host injected nothing."""
    client = standalone_app.test_client()
    assert client.get(URL).status_code == 503
    assert client.post(URL, json={"title": "unauthenticated", "body": ""}).status_code == 503


def test_the_machine_surface_carries_the_scope_attribute_for_openapi(app):
    """The host's OpenAPI generator finds a PAT route by the ``__lotek_scope__`` attribute the
    ``require_scope`` decorator stamps. A route missing it is invisible in the published spec."""
    from bugreport.host import SCOPE_ATTR

    scopes = {
        rule.rule + " " + method: getattr(app.view_functions[rule.endpoint], SCOPE_ATTR, None)
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/bugreport/machine")
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"})
    }
    assert scopes, "no machine routes registered"
    assert all(v in ("read", "write") for v in scopes.values()), scopes
    assert scopes[f"{URL} GET"] == "read"
    assert scopes[f"{URL} POST"] == "write"


def test_the_admin_tombstone_round_trips_to_the_reporter(pat_client, hooks):
    """The #112 feedback leg over the API: an admin tombstones, and the REPORTER can still read it."""
    reporter = StubActor(username="alice")
    hooks["pat_actor"] = reporter
    rid = pat_client.post(URL, json={"title": "agent bug", "body": "x"}).get_json()["report"]["id"]

    hooks["pat_actor"] = StubActor(username="root", role="admin")
    resp = pat_client.patch(f"{URL}/{rid}", json={"status": "deleted", "note": "not a bug"})
    assert resp.status_code == 200

    hooks["pat_actor"] = reporter
    got = pat_client.get(f"{URL}/{rid}").get_json()["report"]
    assert got["status"] == "deleted" and got["admin_note"] == "not a bug"


@pytest.mark.parametrize("payload", [
    {"title": 123, "body": "x"},
    {"title": {"a": 1}, "body": "x"},
    {"title": ["x"], "body": "x"},
    {"title": "ok", "body": 7},
])
def test_a_non_string_field_is_a_400_not_a_500(pat_client, payload):
    """A JSON body is arbitrary: `{"title": 123}` is well-formed JSON. `.strip()` on it raises
    AttributeError and the route 500s (leaking a traceback under a debug config); `str()`-coercing it
    would silently store "{'a': 1}". Refuse it."""
    assert pat_client.post(URL, json=payload).status_code == 400


@pytest.mark.parametrize("bad", [{"status": {"a": 1}}, {"status": ["deleted"]}, {"note": 5}])
def test_a_non_string_admin_field_is_a_400_not_a_500(pat_client, hooks, bad):
    """An UNHASHABLE status raises TypeError out of the enum lookup, not ValueError — a 500, not a 400."""
    hooks["pat_actor"] = StubActor(username="root", role="admin")
    rid = pat_client.post(URL, json={"title": "typed", "body": ""}).get_json()["report"]["id"]
    assert pat_client.patch(f"{URL}/{rid}", json=bad).status_code == 400
    assert loaded(pat_client, rid).status is ReportStatus.open


def test_a_partial_mount_can_only_leave_the_fail_CLOSED_surface_live(tmp_path, monkeypatch):
    """The host injects `cfg.extras` only AFTER `register()` returns, so a blueprint registered before a
    raise stays live with `extras` empty forever. The two surfaces degrade in OPPOSITE directions on an
    empty `extras` — the machine API 503s, the browser page reads it as standalone and treats every
    dashboard user as an admin. So the fail-open one must be registered LAST."""
    from flask import Flask
    from sqlalchemy import create_engine

    import bugreport

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test"
    real = app.register_blueprint
    seen: list[str] = []

    def exploding(blueprint, **kwargs):
        seen.append(blueprint.name)
        if len(seen) == 2:  # whatever is registered SECOND fails to mount
            raise RuntimeError("simulated partial mount")
        return real(blueprint, **kwargs)

    monkeypatch.setattr(app, "register_blueprint", exploding)
    with pytest.raises(RuntimeError):
        bugreport.register(app, create_engine(f"sqlite:///{tmp_path / 'p.db'}", future=True),
                           instance_path=str(tmp_path))

    assert seen[0] == "bugreport_machine", f"the fail-OPEN browser blueprint went live first: {seen}"
    # The survivor refuses rather than serving an unauthenticated, unscoped surface.
    assert app.test_client().get(URL).status_code == 503
