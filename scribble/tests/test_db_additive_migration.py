"""create_all additively adds new model columns to a pre-existing table (Scribble has no migration
framework, and SQLAlchemy create_all only makes missing TABLES, never new COLUMNS), and widens a
SoftHostId column a pre-existing database still stores as INTEGER."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import String, create_engine, inspect, text

from scribble.db import create_all, soft_host_id_columns_typed_integer

#: A real Postgres to prove the widening against, e.g.
#: ``SCRIBBLE_TEST_PG_URL=postgresql+psycopg://scribble:scribble@127.0.0.1:55432/scribble``.
#: The repair is Postgres-only BECAUSE Postgres is the only backend that enforces the column type —
#: which is also why the SQLite suite cannot prove it and these tests skip rather than pretend.
PG_URL = os.environ.get("SCRIBBLE_TEST_PG_URL")


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


# ── SoftHostId columns a pre-existing DB still stores as INTEGER ─────────────────────────────────────


def _legacy_findings_table(conn) -> None:
    """A ``scribble_findings`` from before ``source_finding_id`` became ``SoftHostId`` — the shape that
    took prod down. ``asset_id`` is absent on purpose: create_all ADDS it at the right type, so the only
    column left stale is the one that already existed, which is exactly the asymmetry under test."""
    conn.execute(
        text(
            "CREATE TABLE scribble_findings (id INTEGER PRIMARY KEY, engagement_id INTEGER, "
            "title VARCHAR, source_finding_id INTEGER)"
        )
    )


def test_detects_a_pre_existing_integer_soft_host_id_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with eng.begin() as c:
        _legacy_findings_table(c)
    create_all(eng)
    stale = soft_host_id_columns_typed_integer(eng)
    # SQLite cannot ALTER a column's type and does not need to (dynamic typing), so the column is
    # correctly left alone here — but it must still be REPORTED, or the Postgres repair has nothing to
    # act on and the bug stays invisible to anyone grepping for it.
    assert ("scribble_findings", "source_finding_id") in stale
    # asset_id did not exist on the legacy table, so the additive pass created it at the declared type
    assert ("scribble_findings", "asset_id") not in stale
    create_all(eng)  # idempotent: the SQLite skip must not raise on a second mount


def test_fresh_db_has_no_integer_soft_host_id_columns(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh2.db'}")
    create_all(eng)
    assert soft_host_id_columns_typed_integer(eng) == []


@pytest.mark.skipif(not PG_URL, reason="needs a real Postgres (SCRIBBLE_TEST_PG_URL)")
def test_create_all_widens_a_legacy_integer_column_on_postgres():
    """The whole point, and unprovable on SQLite: on Postgres an INTEGER ``source_finding_id`` refuses
    EVERY insert into ``scribble_findings`` — including one whose ``source_finding_id`` is NULL, because
    psycopg binds the parameter at the type the MODEL declares. Red first, then green through create_all.
    """
    from sqlalchemy.exc import ProgrammingError

    from scribble.db import Base, make_session_factory
    from scribble.models import Engagement, EngagementFinding

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    create_all(eng)  # current schema...
    with eng.begin() as c:  # ...then rewind ONE column to the pre-SoftHostId shape (what prod carried)
        c.execute(text("ALTER TABLE scribble_findings ALTER COLUMN source_finding_id TYPE INTEGER "
                       "USING source_finding_id::integer"))
    assert ("scribble_findings", "source_finding_id") in soft_host_id_columns_typed_integer(eng)

    session_factory = make_session_factory(eng)
    with session_factory() as db:
        db.add(Engagement(name="E"))
        db.commit()
        eid = db.query(Engagement).one().id

    # RED: a minimal finding — nothing UUID-shaped anywhere in it — is refused by the integer column.
    with session_factory() as db, pytest.raises(ProgrammingError, match="source_finding_id"):
        db.add(EngagementFinding(engagement_id=eid, title="minimal", severity="info"))
        db.commit()

    create_all(eng)  # the repair
    assert soft_host_id_columns_typed_integer(eng) == []
    assert isinstance(
        {c["name"]: c["type"] for c in inspect(eng).get_columns("scribble_findings")}["source_finding_id"],
        String,
    )

    # GREEN: the minimal finding lands, AND so does a core UUID ref — the shape the column exists for.
    core_id = uuid.uuid4()
    with session_factory() as db:
        db.add(EngagementFinding(engagement_id=eid, title="minimal", severity="info"))
        db.add(EngagementFinding(
            engagement_id=eid, title="promoted", severity="info", source_finding_id=core_id
        ))
        db.commit()
    with session_factory() as db:
        # SoftHostId round-trips the ORIGINAL type: a uuid.UUID back out, not its string spelling
        promoted = db.query(EngagementFinding).filter_by(title="promoted").one()
        assert promoted.source_finding_id == core_id

    create_all(eng)  # idempotent: nothing left to widen, no error on the next mount
    Base.metadata.drop_all(eng)


@pytest.mark.skipif(not PG_URL, reason="needs a real Postgres (SCRIBBLE_TEST_PG_URL)")
def test_a_failed_widening_degrades_instead_of_unmounting_the_extension(caplog):
    """A repair that cannot run must not take Scribble down with it.

    ``create_all`` runs at MOUNT, and lotek's ``discover_extensions`` swallows every exception by design —
    so a raised ALTER failure would make the extension silently VANISH from the dashboard, which is a
    strictly worse failure than the broken column it was trying to fix (and a far colder trail). Failure
    is injected the way Postgres actually produces it: a VIEW depending on the column, which makes
    ``ALTER COLUMN … TYPE`` refuse outright.

    The view is dropped in a ``finally``: these tests share one scratch database, and a view left behind
    by a failing assertion blocks every later ``DROP TABLE scribble_findings`` with
    ``DependentObjectsStillExist`` — turning one real failure into a cascade of unrelated ones in files
    that never touched it.
    """
    import logging

    from scribble.db import Base

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    create_all(eng)
    with eng.begin() as c:
        c.execute(text("ALTER TABLE scribble_findings ALTER COLUMN source_finding_id TYPE INTEGER "
                       "USING source_finding_id::integer"))
        c.execute(text("CREATE VIEW scribble_findings_pin AS SELECT source_finding_id FROM "
                       "scribble_findings"))

    try:
        with caplog.at_level(logging.ERROR, logger="scribble"):
            create_all(eng)  # must NOT raise

        assert "could not widen" in caplog.text
        assert "ALTER TABLE" in caplog.text  # the message carries the exact SQL to run by hand
        # the column is genuinely still broken -- the point is the honest degrade, not a pretended repair
        assert ("scribble_findings", "source_finding_id") in soft_host_id_columns_typed_integer(eng)
    finally:
        # ALWAYS, even on a failed assertion above: these PG-gated tests share one scratch database, and
        # a leaked view blocks every later `DROP TABLE scribble_findings` with DependentObjectsStillExist
        # — turning one real failure into a cascade of unrelated ones in files that never touched it.
        with eng.begin() as c:
            c.execute(text("DROP VIEW IF EXISTS scribble_findings_pin"))
        Base.metadata.drop_all(eng)
