"""create_all additively adds new model columns to a pre-existing table (Scribble has no migration
framework, and SQLAlchemy create_all only makes missing TABLES, never new COLUMNS)."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from scribble.db import create_all


def test_create_all_adds_missing_columns_to_existing_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    # simulate a scribble_engagements table from an older schema (missing owner_id, client_id, ...)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE scribble_engagements (id INTEGER PRIMARY KEY, name VARCHAR)"))
    create_all(eng)  # must additively add the new columns, not choke on the existing table
    insp = inspect(eng)
    cols = {col["name"] for col in insp.get_columns("scribble_engagements")}
    assert "owner_id" in cols  # the new attribution column is migrated in
    assert "client_id" in cols  # other model columns are added too
    # the DECLARED index on a newly added column must also be created on the upgraded table —
    # create_all skipped the pre-existing table, so only this migration can add it (regression guard).
    indexed = {c for idx in insp.get_indexes("scribble_engagements") for c in idx["column_names"]}
    assert "owner_id" in indexed
    # idempotent: a second run is a no-op (no duplicate-column / duplicate-index error)
    create_all(eng)


def test_create_all_fresh_db_has_owner_id(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    create_all(eng)
    cols = {col["name"] for col in inspect(eng).get_columns("scribble_engagements")}
    assert "owner_id" in cols
