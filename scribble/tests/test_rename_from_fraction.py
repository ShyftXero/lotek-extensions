"""The one-shot ``fraction_* -> scribble_*`` table rename (``db._rename_from_fraction``).

Guards the Fraction -> Scribble extension rename: a database created by the OLD ``fraction_``-prefixed
extension must be migrated in place — tables renamed, rows preserved — and the migration must be a no-op
on a fresh database and safe to run on every boot.
"""

from sqlalchemy import create_engine, inspect, text

from scribble.db import _rename_from_fraction, create_all


def _seed_old_engagements():
    """An engine with a populated OLD-prefixed ``fraction_engagements`` table (id, name only)."""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE fraction_engagements (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO fraction_engagements (id, name) VALUES (1, 'acme')"))
    return engine


def test_rename_migrates_populated_table_in_place():
    engine = _seed_old_engagements()
    _rename_from_fraction(engine)
    names = set(inspect(engine).get_table_names())
    assert "scribble_engagements" in names
    assert "fraction_engagements" not in names  # renamed, not copied
    with engine.begin() as conn:
        assert conn.execute(text("SELECT name FROM scribble_engagements WHERE id = 1")).scalar() == "acme"


def test_rename_is_idempotent_and_noop_on_fresh_db():
    engine = create_engine("sqlite://")
    _rename_from_fraction(engine)          # nothing to rename
    _rename_from_fraction(engine)          # and running again is still safe
    assert inspect(engine).get_table_names() == []


def test_rename_skips_when_target_already_present():
    """If a ``scribble_`` table already exists (a partially-migrated / mixed DB), the old one is left
    alone rather than colliding — the guard is `old present AND new absent`."""
    engine = _seed_old_engagements()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE scribble_engagements (id INTEGER PRIMARY KEY, name TEXT)"))
    _rename_from_fraction(engine)
    names = set(inspect(engine).get_table_names())
    assert "scribble_engagements" in names and "fraction_engagements" in names  # untouched, no crash


def test_create_all_renames_before_building():
    """The full boot path: create_all renames the old table first, so create_all no-ops it instead of
    building an empty ``scribble_engagements`` beside the populated one — and the row survives."""
    engine = _seed_old_engagements()
    create_all(engine)
    names = set(inspect(engine).get_table_names())
    assert "scribble_engagements" in names
    assert "fraction_engagements" not in names
    with engine.begin() as conn:
        assert conn.execute(text("SELECT name FROM scribble_engagements WHERE id = 1")).scalar() == "acme"
