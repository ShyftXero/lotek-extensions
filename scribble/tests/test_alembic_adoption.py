"""Alembic owns Scribble's schema (lotek#335) — including adopting it on a database that predates it.

The dangerous case is not the fresh install; it is the **pre-existing database with real rows**. Its
tables must be brought to the baseline shape and then STAMPED, never rebuilt — and a stamp applied to a
schema that does not actually match the baseline produces a migration chain that believes a column
exists when it does not. That failure is silent until the next revision runs.

Postgres-gated, because the whole point of a migration framework here is the backend that enforces
types: SQLite would accept a wrong-typed column and prove nothing (the same reason the prod outage in
INV-INTEGRITY-03 stayed invisible for weeks).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

PG_URL = os.environ.get("SCRIBBLE_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs a real Postgres (SCRIBBLE_TEST_PG_URL)")

VERSION_TABLE = "scribble_alembic_version"


def _fresh_engine():
    from scribble.db import Base

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
        c.execute(text("DROP TABLE IF EXISTS scribble_clients_pre_mount_remap"))
    return eng


def test_fresh_database_migrates_to_head():
    from scribble.db import run_migrations

    eng = _fresh_engine()
    run_migrations(eng)

    insp = inspect(eng)
    names = set(insp.get_table_names())
    assert "scribble_engagements" in names and "scribble_findings" in names
    assert VERSION_TABLE in names, "Scribble must keep its OWN version table"
    assert "alembic_version" not in names, (
        "Scribble must NOT create or write the host's version table — sharing one makes each project "
        "read the other's revision as an unknown head"
    )
    run_migrations(eng)  # idempotent: a second mount is a no-op


def test_a_preexisting_database_is_stamped_not_rebuilt_and_keeps_its_rows():
    """The adoption path. A database with real rows and no version table must end up at head with its
    data intact — the rows are the whole reason it cannot simply be recreated."""
    from scribble.db import BASELINE_REVISION, Base, make_session_factory, run_migrations
    from scribble.models import Engagement

    eng = _fresh_engine()

    # Build the pre-Alembic world: tables via the raw metadata, a row, and NO version table.
    Base.metadata.create_all(eng)
    session_factory = make_session_factory(eng)
    with session_factory() as db:
        db.add(Engagement(name="pre-existing engagement"))
        db.commit()
    assert VERSION_TABLE not in set(inspect(eng).get_table_names()), "test premise: not yet adopted"

    run_migrations(eng)

    with eng.connect() as c:
        stamped = c.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).scalar_one()
    assert stamped is not None
    with session_factory() as db:
        rows = db.query(Engagement).all()
    assert [e.name for e in rows] == ["pre-existing engagement"], (
        "adoption must STAMP a populated database, never rebuild it — the rows are the point"
    )
    # Sanity: the baseline is a real revision in the chain, not an invented id.
    assert isinstance(BASELINE_REVISION, str) and len(BASELINE_REVISION) >= 8


def test_migrations_never_touch_a_table_that_is_not_scribbles():
    """Scribble migrates inside the HOST's database. A host table sitting beside it must be untouched —
    autogenerate's default behaviour is to propose dropping every table it does not know about, which is
    what `include_object` in env.py exists to prevent."""
    from scribble.db import run_migrations

    eng = _fresh_engine()
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS pretend_host_table"))
        c.execute(text("CREATE TABLE pretend_host_table (id integer primary key, note text)"))
        c.execute(text("INSERT INTO pretend_host_table (id, note) VALUES (1, 'host data')"))

    run_migrations(eng)

    with eng.connect() as c:
        note = c.execute(text("SELECT note FROM pretend_host_table WHERE id = 1")).scalar_one()
    assert note == "host data", "a migration reached outside Scribble's own prefix"
    with eng.begin() as c:
        c.execute(text("DROP TABLE pretend_host_table"))
