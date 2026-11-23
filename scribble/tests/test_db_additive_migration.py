"""create_all additively adds new model columns to a pre-existing table (Scribble has no migration
framework, and SQLAlchemy create_all only makes missing TABLES, never new COLUMNS), and widens a
SoftHostId column a pre-existing database still stores as INTEGER."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import String, create_engine, inspect, text

from scribble.db import (
    _additive_column_sync,
    _widen_soft_host_id_columns,
    soft_host_id_columns_typed_integer,
)


def create_all(engine):
    """The legacy mount path these tests pin, called directly.

    `scribble.db.create_all` now delegates to `run_migrations`, which after adoption also
    applies the UUID revision -- so calling it here would no longer isolate the one-shots
    under test, it would migrate the very schema the test just constructed.
    """
    from scribble.db import Base

    Base.metadata.create_all(engine)
    _additive_column_sync(engine)
    _widen_soft_host_id_columns(engine)

#: A real Postgres to prove the widening against, e.g.
#: ``SCRIBBLE_TEST_PG_URL=postgresql+psycopg://scribble:scribble@127.0.0.1:55432/scribble``.
#: The repair is Postgres-only BECAUSE Postgres is the only backend that enforces the column type —
#: which is also why the SQLite suite cannot prove it and these tests skip rather than pretend.
PG_URL = os.environ.get("SCRIBBLE_TEST_PG_URL")


def test_create_all_adds_missing_columns_to_existing_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    # simulate a scribble_report_boards table from an older schema (missing owner_id, client_id, ...)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE scribble_report_boards (id INTEGER PRIMARY KEY, name VARCHAR)"))
    create_all(eng)  # must additively add the new columns, not choke on the existing table
    insp = inspect(eng)
    cols = {col["name"] for col in insp.get_columns("scribble_report_boards")}
    assert "owner_id" in cols  # the new attribution column is migrated in
    assert "client_id" in cols  # other model columns are added too
    # the DECLARED index on a newly added column must also be created on the upgraded table —
    # create_all skipped the pre-existing table, so only this migration can add it (regression guard).
    indexed = {c for idx in insp.get_indexes("scribble_report_boards") for c in idx["column_names"]}
    assert "owner_id" in indexed
    # idempotent: a second run is a no-op (no duplicate-column / duplicate-index error)
    create_all(eng)


def test_create_all_fresh_db_has_owner_id(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    create_all(eng)
    cols = {col["name"] for col in inspect(eng).get_columns("scribble_report_boards")}
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
    from scribble.models import BoardFinding, ReportBoard

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    create_all(eng)  # current schema...
    with eng.begin() as c:  # ...then rewind ONE column to the pre-SoftHostId shape (what prod carried)
        c.execute(text("ALTER TABLE scribble_findings ALTER COLUMN source_finding_id TYPE INTEGER "
                       "USING source_finding_id::integer"))
    assert ("scribble_findings", "source_finding_id") in soft_host_id_columns_typed_integer(eng)

    session_factory = make_session_factory(eng)
    with session_factory() as db:
        db.add(ReportBoard(name="E"))
        db.commit()
        eid = db.query(ReportBoard).one().id

    # RED: a minimal finding — nothing UUID-shaped anywhere in it — is refused by the integer column.
    with session_factory() as db, pytest.raises(ProgrammingError, match="source_finding_id"):
        db.add(BoardFinding(engagement_id=eid, title="minimal", severity="info"))
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
        db.add(BoardFinding(engagement_id=eid, title="minimal", severity="info"))
        db.add(BoardFinding(
            engagement_id=eid, title="promoted", severity="info", source_finding_id=core_id
        ))
        db.commit()
    with session_factory() as db:
        # SoftHostId round-trips the ORIGINAL type: a uuid.UUID back out, not its string spelling
        promoted = db.query(BoardFinding).filter_by(title="promoted").one()
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


# ── the column shape the additive sync leaves behind (ext#402, Plague r2) ────────────────────────────────

_MACHINE_AUTHORED_REV = "b5e2c8a1f730"
_MACHINE_AUTHORED_PARENT = "c7d9f1b3e5a2"


def _group_columns(engine) -> dict:
    return {c["name"]: c for c in inspect(engine).get_columns("scribble_finding_groups")}


def test_the_machine_authored_revision_tightens_the_column_the_adoption_path_left_nullable(tmp_path):
    """`_additive_column_sync` adds a DEFAULTED model column as a bare nullable ``ADD COLUMN`` — no server
    default — so a pre-Alembic database gets ``machine_authored`` NULLABLE with NULL in every row. The
    model declares it NOT NULL. The revision that owns the column used to guard on PRESENCE alone and
    no-op, leaving that shape in place forever: harmless today (a NULL reads falsy, the conservative
    "hand-authored"), but a future ``WHERE machine_authored IS FALSE`` reader would silently drop every
    such row — the SoftHostId drift class again.

    Drives the REAL adoption-path code (`_additive_column_sync`) and the REAL revision, so the premise is
    what a legacy deployment actually looks like rather than a hand-built table.
    """
    from alembic import command
    from sqlalchemy.exc import IntegrityError

    from scribble.db import Base, _alembic_config, make_session_factory, run_migrations
    from scribble.models import FindingGroup, ReportBoard

    eng = create_engine(f"sqlite:///{tmp_path / 'adopted.db'}")
    run_migrations(eng)  # fresh: built from the models, stamped at head

    session_factory = make_session_factory(eng)
    with session_factory() as db:
        board = ReportBoard(name="E")
        db.add(board)
        db.flush()
        db.add(FindingGroup(engagement_id=board.id, name="External"))
        db.commit()

    # Rewind to the legacy shape through the code, not by hand: drop the column, then let the adoption
    # sync re-add it exactly as it does on a pre-Alembic database.
    with eng.begin() as c:
        c.execute(text("ALTER TABLE scribble_finding_groups DROP COLUMN machine_authored"))
    _additive_column_sync(eng)

    col = _group_columns(eng)["machine_authored"]
    assert col["nullable"] is True, "premise: the sync no longer leaves a defaulted column nullable"
    with eng.connect() as c:
        assert c.execute(text(
            "SELECT machine_authored FROM scribble_finding_groups WHERE name = 'External'"
        )).scalar_one() is None, "premise: the sync backfills nothing — the existing row is NULL"

    with eng.begin() as c:
        command.stamp(_alembic_config(c), _MACHINE_AUTHORED_PARENT)  # this revision now pending
    run_migrations(eng)

    col = _group_columns(eng)["machine_authored"]
    assert col["nullable"] is False, (
        "the revision no-opped on presence alone: the model declares NOT NULL while the database kept a "
        "nullable column full of NULLs"
    )
    with eng.connect() as c:
        assert c.execute(text(
            "SELECT machine_authored FROM scribble_finding_groups WHERE name = 'External'"
        )).scalar_one() in (0, False), "the pre-existing row was not backfilled to false"
    # ...and the constraint is enforced by the database, not merely declared in reflected metadata.
    with pytest.raises(IntegrityError), eng.begin() as c:
        c.execute(text("UPDATE scribble_finding_groups SET machine_authored = NULL"))

    # Idempotent: a second mount is a no-op (this revision is now the recorded head).
    run_migrations(eng)
    assert _group_columns(eng)["machine_authored"]["nullable"] is False
    Base.metadata.drop_all(eng)
