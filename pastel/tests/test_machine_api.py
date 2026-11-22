"""PAT machine API: create / list / get / delete, scope enforcement, and the no-host fail-closed case.

The `pat_client` fixture blanks the browser session, so a route that accidentally read `current_actor`
instead of the PAT principal would fail here.
"""

from __future__ import annotations

import uuid

from pastel.models import Paste


def _created(pat_client, **body):
    body.setdefault("body", "# notes")
    return pat_client.post("/pastel/machine/pastes", json=body)


def test_create_returns_guid_and_links(pat_client):
    resp = _created(pat_client, title="scan", body="hello", max_reads=3, expires_in="1d")
    assert resp.status_code == 201, resp.data
    paste = resp.get_json()["paste"]
    assert paste["guid"]
    assert paste["url"].endswith("/pastel/p/" + paste["guid"])
    assert paste["raw_url"].endswith("/raw")
    assert paste["max_reads"] == 3
    assert paste["password_protected"] is False


def test_created_paste_is_attributed_to_the_token_user(pat_client, hooks, session_factory):
    resp = _created(pat_client)
    pid = uuid.UUID(resp.get_json()["paste"]["id"])
    with session_factory() as db:
        assert db.get(Paste, pid).author_id == hooks["pat_actor"].id


def test_password_set_by_runner_is_hashed(pat_client, session_factory):
    resp = _created(pat_client, password="hunter2")
    pid = uuid.UUID(resp.get_json()["paste"]["id"])
    with session_factory() as db:
        row = db.get(Paste, pid)
        assert row.password_hash and "hunter2" not in row.password_hash


def test_list_only_shows_own_pastes(pat_client, session_factory):
    _created(pat_client, body="mine")
    # A paste owned by someone else must not appear.
    with session_factory() as db:
        from pastel.service import create
        create(db, author_id=uuid.uuid7(), title="other", body="theirs")
    rows = pat_client.get("/pastel/machine/pastes").get_json()["pastes"]
    assert all(r["title"] != "other" for r in rows)
    assert any(r["title"] is None or r["guid"] for r in rows)


def test_get_other_users_paste_is_404(pat_client, session_factory):
    with session_factory() as db:
        from pastel.service import create
        other = create(db, author_id=uuid.uuid7(), title="x", body="y")
        oid = other.id
    assert pat_client.get(f"/pastel/machine/pastes/{oid}").status_code == 404


def test_delete_own_paste(pat_client, session_factory):
    pid = _created(pat_client).get_json()["paste"]["id"]
    assert pat_client.delete(f"/pastel/machine/pastes/{pid}").status_code == 200
    with session_factory() as db:
        assert db.get(Paste, uuid.UUID(pid)) is None


def test_read_scope_cannot_write(pat_client, hooks):
    """A token lacking `write` is refused by POST — proves the route declares the right scope."""
    hooks["pat_actor"] = type(hooks["pat_actor"])(scopes=frozenset({"read"}))
    assert _created(pat_client).status_code == 403


def test_empty_body_is_400(pat_client):
    assert _created(pat_client, body="   ").status_code == 400


def test_bad_ttl_is_400(pat_client):
    assert _created(pat_client, expires_in="whenever").status_code == 400


def test_machine_api_fails_closed_without_host(pat_client, hooks):
    """No PAT authenticator injected -> 503, never an unauthenticated run."""
    app = pat_client.application
    app.extensions["pastel"].extras.pop("pat_authenticate", None)
    app.extensions["pastel"].extras.pop("require_pat_scope", None)
    assert pat_client.get("/pastel/machine/pastes").status_code == 503
