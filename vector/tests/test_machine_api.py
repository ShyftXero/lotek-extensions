"""The PAT machine API (``vector/api_pat.py``) — scope gating, owner-scoped tenancy against the TOKEN's
principal, builtin read-only, and the HTML export an agent attaches to a report.

These are Vector's OWN proofs. The host's token/scope *scheme* is lotek's concern (proven there against the
real authenticator); what must be proven HERE is that Vector declares the right scope on every machine
route, applies its own visibility rule to the PAT principal rather than a session that does not exist on
this surface, and keeps the seeded examples read-only.
"""

from __future__ import annotations

import json
import uuid

import pytest
from conftest import FakeUser, StubActor, login

from vector.host import SCOPE_ATTR
from vector.models import Diagram

MACHINE = "/vector/machine"

# Phase headings are `title` and node labels are `label` in vector.attackpath/v1 (see
# vector/schema.py::_norm_phase / _norm_node) — normalize() drops anything it doesn't recognize.
_MODEL = {
    "phases": [{"title": "Initial Access"}],
    "nodes": [{"id": "n1", "label": "phish"}],
}


def _machine_rules(app):
    return [r for r in app.url_map.iter_rules() if str(r.rule).startswith(MACHINE)]


def _create(pat_client, name="Path A", model=None):
    res = pat_client.post(f"{MACHINE}/diagrams", json={"name": name, "model": model or _MODEL})
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()


# ── the load-bearing invariant ────────────────────────────────────────────────────────────────────────


def test_every_machine_route_is_scope_gated(app):
    """No route on this surface may be reachable by a merely-authenticated token.

    Walks the real ``url_map`` rather than a hand-kept list, so a route added later without
    ``@host.require_scope`` fails this test instead of shipping ungated.
    """
    rules = _machine_rules(app)
    assert rules, "no /vector/machine routes are registered at all"
    ungated = [
        str(r.rule) for r in rules
        if not hasattr(app.view_functions[r.endpoint], SCOPE_ATTR)
    ]
    assert ungated == [], f"machine routes missing require_scope: {ungated}"


def test_the_machine_prefix_does_not_overlap_the_browser_api(app):
    """The host exempts the machine prefix from CSRF and its session gate. That is only sound while the
    prefix stays disjoint from the cookie-authed browser surface."""
    browser = [str(r.rule) for r in app.url_map.iter_rules() if str(r.rule).startswith("/vector/api")]
    assert browser, "the browser api should still be mounted"
    assert not any(r.startswith(MACHINE) for r in browser)


# ── scope gating ─────────────────────────────────────────────────────────────────────────────────────


def test_read_token_cannot_write(app, pat_client):
    """A read-scoped token is refused by every write route, and writes nothing."""
    app.pat["actor"] = StubActor(scopes=frozenset({"read"}))
    assert pat_client.post(f"{MACHINE}/diagrams", json={"name": "Nope"}).status_code == 403
    assert pat_client.get(f"{MACHINE}/diagrams").get_json()["diagrams"] == []


def test_read_token_can_read(pat_client):
    assert pat_client.get(f"{MACHINE}/diagrams").status_code == 200


def test_write_routes_each_reject_a_read_only_token(app, pat_client):
    """Every mutating verb, not just create — a scope check missing from PUT or DELETE would be just as
    exploitable as one missing from POST."""
    created = _create(pat_client)
    app.pat["actor"] = StubActor(scopes=frozenset({"read"}))
    assert pat_client.put(f"{MACHINE}/diagrams/{created['id']}", json={"name": "x"}).status_code == 403
    assert pat_client.delete(f"{MACHINE}/diagrams/{created['id']}").status_code == 403


def test_unauthenticated_token_is_refused(app, pat_client):
    """The host's ``pat_authenticate`` result is honoured as-is by the blueprint's before_request."""
    app.pat["authenticate"] = ({"error": "unauthorized"}, 401)
    assert pat_client.get(f"{MACHINE}/diagrams").status_code == 401


def test_fails_closed_when_no_host_injected_the_pat_hooks(app, pat_client):
    """Unmounted (or a host that injected nothing) must be a 503, never an open door."""
    app.extensions["vector"].extras.pop("pat_authenticate")
    res = pat_client.get(f"{MACHINE}/diagrams")
    assert res.status_code == 503
    assert res.get_json()["error"] == "unavailable"


# ── tenancy against the TOKEN's principal ─────────────────────────────────────────────────────────────


def test_created_diagram_is_owned_by_the_pat_actor_not_the_session(app, pat_client):
    """The regression this surface exists to avoid: attributing a machine write to ``current_actor``,
    which is None on a PAT request. ``pat_client`` blanks the session so that mistake cannot pass."""
    actor = app.pat["actor"]
    created = _create(pat_client, name="Agent path")
    with app.extensions["vector"].session_factory() as db:
        row = db.get(Diagram, uuid.UUID(created["id"]))
        assert row.owner_id == actor.id
        assert row.owner_id is not None
        assert row.created_by == actor.username


def test_the_creating_token_can_read_its_own_diagram_back(pat_client):
    """The end-to-end point of owner attribution: an agent that writes a diagram must be able to fetch it
    again. A NULL owner would 404 here even though the write succeeded."""
    created = _create(pat_client, name="Round trip")
    res = pat_client.get(f"{MACHINE}/diagrams/{created['id']}")
    assert res.status_code == 200
    assert res.get_json()["name"] == "Round trip"


def test_another_tokens_diagram_is_invisible_and_404s(app, pat_client):
    """Not 403 — a 403 would confirm the id exists, which is what an id-guessing probe wants."""
    created = _create(pat_client, name="Mine")
    app.pat["actor"] = StubActor(id=uuid.UUID(int=99), username="other")
    assert pat_client.get(f"{MACHINE}/diagrams").get_json()["diagrams"] == []
    assert pat_client.get(f"{MACHINE}/diagrams/{created['id']}").status_code == 404
    assert pat_client.put(f"{MACHINE}/diagrams/{created['id']}", json={"name": "x"}).status_code == 404
    assert pat_client.delete(f"{MACHINE}/diagrams/{created['id']}").status_code == 404


def test_admin_token_sees_every_diagram(app, pat_client):
    created = _create(pat_client, name="Mine")
    app.pat["actor"] = StubActor(id=uuid.UUID(int=99), username="root", role="admin")
    ids = [d["id"] for d in pat_client.get(f"{MACHINE}/diagrams").get_json()["diagrams"]]
    assert created["id"] in ids


def test_a_session_user_cannot_reach_another_users_machine_written_diagram(app, pat_client):
    """The two identities stay separate: a diagram owned by token principal 7 is not handed to a logged-in
    browser user who happens to be a different id."""
    created = _create(pat_client, name="Token owned")
    login(app, FakeUser(uid=uuid.UUID(int=42), username="someone-else"))
    assert app.test_client().get(f"/vector/api/diagrams/{created['id']}").status_code == 404


def test_get_missing_diagram_is_404(pat_client):
    assert pat_client.get(f"{MACHINE}/diagrams/{uuid.uuid4()}").status_code == 404


# ── a non-UUID principal id degrades loudly, pinned so it is documented and not silently wrong ─────────


def test_a_non_uuid_principal_id_degrades_loudly_to_a_null_owner(app, pat_client, caplog):
    """`Diagram.owner_id` is a `Uuid` column and lotek's core `User.id` is a UUIDv7, so a real mounted
    principal id IS a `uuid.UUID` and is stored. A principal whose id is NOT a `uuid.UUID` (a legacy int,
    say) cannot be bound into that column. The contract is: warn and store NULL — never coerce a non-UUID
    into the owner column, and never silently pretend it worked.

    This mirrors the cookie surface's guard (`deps.current_actor_id` returns None for a non-`uuid.UUID`
    id); this test is what fails and points at `_actor_owner_id` if that ever regresses.
    """
    app.pat["actor"] = StubActor(id=999)  # type: ignore[arg-type]  # a non-UUID id cannot own a Uuid row
    with caplog.at_level("WARNING", logger="vector"):
        created = _create(pat_client, name="Legacy-int principal")
    assert "not a uuid.UUID" in caplog.text, caplog.text
    with app.extensions["vector"].session_factory() as db:
        assert db.get(Diagram, uuid.UUID(created["id"])).owner_id is None


def test_a_principal_whose_id_does_not_fit_lists_builtin_only(make_app):
    """List and get must agree. A principal whose id is not a ``uuid.UUID`` resolves to a None owner id;
    comparing against it would render as ``owner_id IS NULL`` and list exactly the null-owner rows that a
    direct fetch refuses — so such a principal must see builtin-only."""
    app = make_app(seed=True)
    login(app, None)
    app.pat["actor"] = StubActor(id=999)  # type: ignore[arg-type]  # a non-UUID id cannot own a Uuid row
    client = app.test_client()
    diagrams = client.get(f"{MACHINE}/diagrams").get_json()["diagrams"]
    assert diagrams, "the builtin example should still be visible"
    assert all(d["builtin"] for d in diagrams)


# ── builtin examples are read-only ────────────────────────────────────────────────────────────────────


@pytest.fixture
def seeded_app(make_app):
    app = make_app(seed=True)
    login(app, None)
    return app


def test_builtin_examples_are_visible_but_not_writable(seeded_app):
    client = seeded_app.test_client()
    builtin = [d for d in client.get(f"{MACHINE}/diagrams").get_json()["diagrams"] if d["builtin"]]
    assert builtin, "the seed should have produced a builtin example"
    bid = builtin[0]["id"]
    assert client.get(f"{MACHINE}/diagrams/{bid}").status_code == 200
    assert client.put(f"{MACHINE}/diagrams/{bid}", json={"name": "hijacked"}).status_code == 403
    assert client.delete(f"{MACHINE}/diagrams/{bid}").status_code == 403


# ── round-trip + export ──────────────────────────────────────────────────────────────────────────────


def test_update_replaces_name_and_model(pat_client):
    created = _create(pat_client, name="Before")
    res = pat_client.put(f"{MACHINE}/diagrams/{created['id']}",
                         json={"name": "After", "model": {"phases": [{"title": "Exfil"}], "nodes": []}})
    assert res.status_code == 200
    fetched = pat_client.get(f"{MACHINE}/diagrams/{created['id']}").get_json()
    assert fetched["name"] == "After"
    assert [p["title"] for p in fetched["model"]["phases"]] == ["Exfil"]


def test_delete_removes_the_diagram(pat_client):
    created = _create(pat_client)
    assert pat_client.delete(f"{MACHINE}/diagrams/{created['id']}").status_code == 200
    assert pat_client.get(f"{MACHINE}/diagrams/{created['id']}").status_code == 404


def test_the_model_is_normalized_on_the_way_in(pat_client):
    """Whatever an agent posts is stored through ``schema.normalize``, so the machine surface cannot seed
    a shape the renderer would then choke on."""
    created = _create(pat_client, model={"nodes": [{"id": "n1", "label": "x"}]})
    with_model = pat_client.get(f"{MACHINE}/diagrams/{created['id']}").get_json()["model"]
    assert isinstance(with_model, dict)
    assert "phases" in with_model and "nodes" in with_model


def test_export_returns_a_self_contained_html_attachment(pat_client):
    """The deliverable an agent attaches to a report."""
    created = _create(pat_client, name="Report path")
    res = pat_client.get(f"{MACHINE}/diagrams/{created['id']}/export.html")
    assert res.status_code == 200
    assert res.mimetype == "text/html"
    assert "attachment" in res.headers["Content-Disposition"]
    assert "Report-path.html" in res.headers["Content-Disposition"]
    body = res.get_data(as_text=True)
    assert "<html" in body.lower()
    assert "http://" not in body.replace("http://www.w3.org", "")  # no remote fetches in a deliverable


def test_export_of_another_tokens_diagram_is_404(app, pat_client):
    created = _create(pat_client)
    app.pat["actor"] = StubActor(id=uuid.UUID(int=99), username="other")
    assert pat_client.get(f"{MACHINE}/diagrams/{created['id']}/export.html").status_code == 404


def test_name_is_capped_and_defaulted(pat_client):
    created = _create(pat_client, name="x" * 500)
    assert len(created["name"]) == 200
    unnamed = pat_client.post(f"{MACHINE}/diagrams", json={"model": _MODEL}).get_json()
    assert unnamed["name"] == "Untitled attack path"


def test_create_tolerates_a_missing_model(pat_client):
    """An agent that creates the diagram first and fills it in later must not get a 500."""
    res = pat_client.post(f"{MACHINE}/diagrams", json={"name": "Empty"})
    assert res.status_code == 201
    model = pat_client.get(f"{MACHINE}/diagrams/{res.get_json()['id']}").get_json()["model"]
    assert json.dumps(model)  # serializable, whatever normalize made of it
