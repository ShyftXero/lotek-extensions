"""Report-authz IDOR regression.

``Engagement.client_id`` is a *soft* int reference with no id-space: in a standalone->mounted Scribble DB
a ``scribble_clients`` id is misread as a host ``clients`` id and can collide with a real host client an
attacker owns a job under, exposing that client's report. ``scribble.db._remap_standalone_client_ids``
(run at the end of ``create_all``) remaps those ids to host space by client NAME so the host authz
compare is correct.

The security assertion is the collision case: a scribble client whose id EQUALS a *different* host
client's id must remap to the host client of the SAME NAME, not stay on the colliding id.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import Column, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase

import scribble.db as sdb
import scribble.models  # noqa: F401 — registers the scribble_* tables on Base.metadata


def _host_client_model():
    """A throwaway host ``clients`` model (id + name), standing in for lotek's Client. Classic ``Column``
    (not ``Mapped[...]``) so a function-local mapped class needs no module-level annotation resolution."""

    class HostBase(DeclarativeBase):
        pass

    class HostClient(HostBase):
        __tablename__ = "clients"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    return HostClient


def _mounted_ctx(host_model):
    """A Flask app context whose ``scribble`` config exposes ``client_model`` (mounted mode)."""
    from flask import Flask

    app = Flask(__name__)
    app.extensions["scribble"] = SimpleNamespace(client_model=host_model)
    return app.app_context()


def _seed_standalone(engine, host_model):
    """scribble tables + a host clients table, planted with a NAME collision: scribble client id 1 =
    'Acme', but host client id 1 = 'Zeta' (the attacker's); the real host 'Acme' is id 5."""
    # Build the INT-PK schema by hand, not from `Base.metadata`. This test pins behaviour from BEFORE
    # the UUID migration (lotek#335): `_remap_standalone_client_ids` runs during Alembic adoption, on a
    # legacy standalone database whose PKs are still integers. Today's metadata would create UUID
    # columns, and the int ids below would be stored as text — a world that never existed.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE scribble_clients (id INTEGER PRIMARY KEY, name VARCHAR(255), "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE scribble_engagements (id INTEGER PRIMARY KEY, client_id VARCHAR(64), "
            "name VARCHAR(255), scope_type VARCHAR(64), status VARCHAR(32), "
            "created_at DATETIME, updated_at DATETIME)"
        ))
    host_model.metadata.create_all(engine)
    ts = "2026-01-01 00:00:00"  # raw SQL bypasses the ORM's utcnow() default; supply the NOT-NULL stamps
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO clients (id, name) VALUES (5, 'Acme'), (1, 'Zeta')"))
        conn.execute(text(
            "INSERT INTO scribble_clients (id, name, created_at, updated_at) VALUES "
            f"(1, 'Acme', '{ts}', '{ts}'), (2, 'Beta', '{ts}', '{ts}'), (3, 'Gamma', '{ts}', '{ts}')"
        ))
        conn.execute(text(
            "INSERT INTO scribble_engagements "
            "(id, client_id, name, scope_type, status, created_at, updated_at) VALUES "
            f"(10, 1, 'e-acme', 'external', 'in_progress', '{ts}', '{ts}'), "
            f"(11, 2, 'e-beta', 'external', 'in_progress', '{ts}', '{ts}'), "
            f"(12, 3, 'e-gamma', 'external', 'in_progress', '{ts}', '{ts}')"
        ))


def _client_ids(engine):
    """Raw-SQL read of ``client_id``, coerced back to int where possible.

    ``client_id`` is ``scribble.db.SoftHostId`` (TEXT-backed, since v2 widened it to also hold a UUID
    host id) -- a raw SQL fetch bypasses the ORM's type decoder, so SQLite's TEXT-affinity storage hands
    back e.g. ``'5'`` for what was written as the plain int ``5``. This test operates at the raw-SQL
    level deliberately (see module docstring), but the ids it's asserting on are genuinely int-shaped in
    every case here (a legacy standalone->mounted int host) -- coerce back to what any real ORM read
    (``Engagement.client_id``) would hand back, rather than asserting on SQLite's storage class.
    """
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, client_id FROM scribble_engagements ORDER BY id")).fetchall()
    return {eid: (int(cid) if cid is not None else None) for eid, cid in rows}


def test_remap_resolves_collision_by_name_and_nulls_unmatched():
    HostClient = _host_client_model()
    engine = create_engine("sqlite://")
    _seed_standalone(engine, HostClient)

    with _mounted_ctx(HostClient):
        sdb._remap_standalone_client_ids(engine)  # the function under test, called directly

    ids = _client_ids(engine)
    # SECURITY: engagement 10's client was scribble 'Acme' (id 1). Host id 1 is a DIFFERENT client
    # ('Zeta'); the real host 'Acme' is id 5. The remap must move it to 5 by NAME — NOT leave it on the
    # colliding id 1, which is the IDOR.
    assert ids[10] == 5, "collision must resolve to the host client of the same NAME, not the colliding id"
    # 'Beta' / 'Gamma' have no host client of that name -> NULL -> admin-only report (secure default).
    assert ids[11] is None
    assert ids[12] is None
    assert "scribble_clients_pre_mount_remap" in set(inspect(engine).get_table_names())


def test_remap_is_idempotent():
    HostClient = _host_client_model()
    engine = create_engine("sqlite://")
    _seed_standalone(engine, HostClient)
    with _mounted_ctx(HostClient):
        sdb._remap_standalone_client_ids(engine)
        first = _client_ids(engine)
        sdb.Base.metadata.create_all(engine)  # second boot recreates an empty scribble_clients
        sdb._remap_standalone_client_ids(engine)  # -> no-op
        second = _client_ids(engine)
    assert first == second == {10: 5, 11: None, 12: None}
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM scribble_clients")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM scribble_clients_pre_mount_remap")).scalar() == 3


def test_standalone_is_untouched():
    """Standalone (no host client_model) -> scribble_clients IS the real store; the remap must no-op."""
    engine = create_engine("sqlite://")
    _seed_standalone(engine, _host_client_model())  # seeds a clients table; NOT injected as client_model
    with _mounted_ctx(None):  # client_model None -> deps.client_model() returns scribble's own Client
        sdb._remap_standalone_client_ids(engine)
    assert _client_ids(engine) == {10: 1, 11: 2, 12: 3}
    assert "scribble_clients_pre_mount_remap" not in set(inspect(engine).get_table_names())
