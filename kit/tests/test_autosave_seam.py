"""The unified autosave seam (lotek_kit.autosave) — the hardened, fail-closed path a generic write endpoint
has to be. Pins: authz is MANDATORY at register time; the endpoint refuses an unknown kind (404) and an
unauthorized actor (403); an authorized actor round-trips save + load; the kind owns storage while the seam
owns dispatch + the gate. This IS the tenancy invariant for the seam — a kind that forgets authz, or a
dispatch that lets an unauthorized write through, fails HERE, not in prod.
"""
from __future__ import annotations

import pytest
from flask import Flask

from lotek_kit import autosave


@pytest.fixture(autouse=True)
def _clean_registry():
    autosave._KINDS.clear()
    yield
    autosave._KINDS.clear()


def _app(actor, *, is_doc=None) -> Flask:
    app = Flask(__name__)
    autosave.ensure_autosave_registered(app, actor=lambda: actor, is_doc=is_doc)
    return app


# ── register-time: authz (and load/save) are mandatory ──────────────────────────────────────────────

def test_register_requires_authorize():
    with pytest.raises(ValueError, match="authorize"):
        autosave.register_autosave_kind("x", load=lambda i, b: ({}, ""), save=lambda i, b, d: "", authorize=None)


def test_register_requires_callables():
    with pytest.raises(ValueError):
        autosave.register_autosave_kind("x", load=None, save=lambda i, b, d: "", authorize=lambda a, i: True)
    with pytest.raises(ValueError):
        autosave.register_autosave_kind("", load=lambda i, b: ({}, ""), save=lambda i, b, d: "",
                                        authorize=lambda a, i: True)


# ── the endpoint: fail closed, then round-trip ──────────────────────────────────────────────────────

def test_unknown_kind_is_refused_404():
    client = _app(actor="u1").test_client()
    r = client.post("/_kit/autosave", json={"kind": "nope", "id": "1", "block": "description", "doc": {}})
    assert r.status_code == 404 and r.get_json()["ok"] is False


def test_unauthorized_actor_is_refused_403_and_never_saves():
    saved: list = []
    autosave.register_autosave_kind(
        "finding",
        load=lambda i, b: ({"type": "doc"}, "<p></p>"),
        save=lambda i, b, d: saved.append((i, b, d)) or "<p>saved</p>",
        authorize=lambda actor, i: actor == "owner",       # only the owner may write
    )
    client = _app(actor="intruder").test_client()
    r = client.post("/_kit/autosave", json={"kind": "finding", "id": "1", "block": "description", "doc": {"type": "doc"}})
    assert r.status_code == 403
    assert saved == [], "an unauthorized write must never reach the kind's save"


def test_authorized_actor_round_trips_save_and_load():
    store: dict[tuple[str, str], object] = {}
    autosave.register_autosave_kind(
        "finding",
        load=lambda i, b: (store.get((i, b), {"type": "doc", "content": []}), "<p>cached</p>"),
        save=lambda i, b, d: (store.__setitem__((i, b), d), "<p>fresh</p>")[1],
        authorize=lambda actor, i: actor == "owner",
    )
    client = _app(actor="owner").test_client()

    doc = {"type": "doc", "content": [{"type": "paragraph"}]}
    r = client.post("/_kit/autosave", json={"kind": "finding", "id": "42", "block": "description", "doc": doc})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "html": "<p>fresh</p>"}
    assert store[("42", "description")] == doc                        # the kind's save ran

    r = client.get("/_kit/autosave", query_string={"kind": "finding", "id": "42", "block": "description"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True and body["doc"] == doc   # load returns what save stored


def test_missing_handle_fields_are_400():
    autosave.register_autosave_kind("finding", load=lambda i, b: ({}, ""), save=lambda i, b, d: "",
                                    authorize=lambda a, i: True)
    client = _app(actor="owner").test_client()
    assert client.post("/_kit/autosave", json={"kind": "finding", "id": "1"}).status_code == 400  # no block


def test_is_doc_validator_rejects_bad_save_body():
    autosave.register_autosave_kind("finding", load=lambda i, b: ({}, ""), save=lambda i, b, d: "ok",
                                    authorize=lambda a, i: True)
    client = _app(actor="owner", is_doc=lambda d: isinstance(d, dict) and d.get("type") == "doc").test_client()
    bad = client.post("/_kit/autosave", json={"kind": "finding", "id": "1", "block": "b", "doc": "nope"})
    assert bad.status_code == 400
    good = client.post("/_kit/autosave", json={"kind": "finding", "id": "1", "block": "b", "doc": {"type": "doc"}})
    assert good.status_code == 200


def test_mount_is_idempotent_first_caller_wins():
    app = Flask(__name__)
    p1 = autosave.ensure_autosave_registered(app, actor=lambda: "u")
    p2 = autosave.ensure_autosave_registered(app, actor=lambda: "u", url_prefix="/other")
    assert p1 == "/_kit/autosave" and p2 == "/_kit/autosave"   # second call no-ops, returns the live prefix
