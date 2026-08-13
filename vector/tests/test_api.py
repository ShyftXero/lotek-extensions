"""JSON API — CRUD, owner-scoped visibility / IDOR guards, write gating, import round-trip."""

from __future__ import annotations

import uuid

from conftest import FakeUser, login

from vector.schema import blank_model

# v2 host actors are keyed on UUIDv7 (see vector.deps.current_actor_id) — the fake actors must be
# uuid.UUID-typed or owner scoping silently collapses to None, exactly what the deps guard now refuses.
OP_A = FakeUser(uuid.UUID(int=1), "alice", "operator")
OP_B = FakeUser(uuid.UUID(int=2), "bob", "operator")
VIEWER = FakeUser(uuid.UUID(int=3), "val", "viewer")
ADMIN = FakeUser(uuid.UUID(int=9), "admin", "admin")


def _create(client, name="D", model=None):
    return client.post("/vector/api/diagrams", json={"name": name, "model": model or blank_model(name)})


def test_create_get_update_delete_as_owner(app, client):
    login(app, OP_A)
    r = _create(client, "Mine")
    assert r.status_code == 201
    did = r.get_json()["id"]

    assert client.get(f"/vector/api/diagrams/{did}").status_code == 200
    assert client.put(f"/vector/api/diagrams/{did}", json={"name": "Renamed"}).status_code == 200
    assert client.get(f"/vector/api/diagrams/{did}").get_json()["name"] == "Renamed"
    assert client.delete(f"/vector/api/diagrams/{did}").status_code == 200
    assert client.get(f"/vector/api/diagrams/{did}").status_code == 404


def test_idor_other_user_cannot_see_or_modify(app, client):
    login(app, OP_A)
    did = _create(client, "A-private").get_json()["id"]

    login(app, OP_B)
    assert client.get(f"/vector/api/diagrams/{did}").status_code == 404  # not disclosed
    assert client.put(f"/vector/api/diagrams/{did}", json={"name": "hijack"}).status_code == 404
    assert client.delete(f"/vector/api/diagrams/{did}").status_code == 404
    # B's list must not contain A's private diagram
    ids = [d["id"] for d in client.get("/vector/api/diagrams").get_json()["diagrams"]]
    assert did not in ids


def test_admin_sees_and_modifies_any(app, client):
    login(app, OP_A)
    did = _create(client, "A-private").get_json()["id"]
    login(app, ADMIN)
    assert client.get(f"/vector/api/diagrams/{did}").status_code == 200
    assert client.put(f"/vector/api/diagrams/{did}", json={"name": "admin-edit"}).status_code == 200


def test_viewer_cannot_write(app, client):
    login(app, VIEWER)
    assert _create(client, "nope").status_code == 403


def test_builtin_example_is_read_only_but_duplicable(make_app):
    app = make_app(seed=True)
    client = app.test_client()
    login(app, OP_A)
    diags = client.get("/vector/api/diagrams").get_json()["diagrams"]
    builtin = [d for d in diags if d["builtin"]]
    assert builtin, "seeded example should be visible to all users"
    bid = builtin[0]["id"]
    assert client.put(f"/vector/api/diagrams/{bid}", json={"name": "x"}).status_code == 403
    assert client.delete(f"/vector/api/diagrams/{bid}").status_code == 403
    dup = client.post(f"/vector/api/diagrams/{bid}/duplicate")
    assert dup.status_code == 201
    new_id = dup.get_json()["id"]
    got = client.get(f"/vector/api/diagrams/{new_id}").get_json()
    assert got["builtin"] is False and "(copy)" in got["name"]


def test_import_then_export_roundtrip_is_identity(app, client):
    login(app, OP_A)
    from vector.schema import normalize
    from vector.seed import _model

    src = normalize(_model())
    did = client.post("/vector/api/import", json={"name": "Imported", "model": src}).get_json()["id"]
    exported = client.get(f"/vector/diagrams/{did}/export.json")
    import json

    got = json.loads(exported.data)
    assert got == src  # normalize is idempotent, so import->store->export round-trips exactly


def test_export_html_endpoint_serves_self_contained(app, client):
    login(app, OP_A)
    did = _create(client, "HtmlDoc").get_json()["id"]
    r = client.get(f"/vector/diagrams/{did}/export.html")
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert b"__VECTOR_MODEL__" in r.data
    assert b"<link" not in r.data
