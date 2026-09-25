"""The Report Board rename migration (`d1e2f3a4b5c6`) must self-heal duplicate `core_engagement_id`
links BEFORE it builds the 1:1 partial-unique index — otherwise an existing database that already holds
two boards for one core engagement aborts the whole migration, and scribble fails to mount.

This is the exact prod outage behind lotek#914's neighbourhood: two "TeamsPlus" report boards linked the
same core engagement (created before the constraint, via the machine create path that inserted
unconditionally), so `CREATE UNIQUE INDEX uq_scribble_report_board_core_engagement` raised
UniqueViolation, the migration transaction rolled back, and the mount died. The migration now keeps the
oldest board per core id and nulls the link on the rest (they survive as orphan boards with their
findings intact), matching the by-core resolver's oldest-wins rule.

Postgres-gated for the same reason as the sibling migration tests: the unique index + real transactional
DDL are the point (SQLite would not reproduce the prod backend).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect, text

PG_URL = os.environ.get("SCRIBBLE_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs a real Postgres (SCRIBBLE_TEST_PG_URL)")

VERSION_TABLE = "scribble_alembic_version"
_RENAME_REV = "d1e2f3a4b5c6"
_BEFORE_RENAME = "c9e1a2b3d4f5"  # down_revision of the rename+index migration
_UQ = "uq_scribble_report_board_core_engagement"

_CORE = "01a00000-0000-7000-8000-0000000000aa"
_KEEP = "01a00000-0000-7000-8000-000000000001"  # smaller id -> oldest -> keeps the link
_DROP = "01a00000-0000-7000-8000-000000000002"  # nulled, survives as an orphan board


def _fresh_engine():
    from scribble.db import Base

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
        c.execute(text("DROP TABLE IF EXISTS scribble_pk_migration_map CASCADE"))
    return eng


def test_rename_migration_deduplicates_core_engagement_links_then_builds_the_unique_index():
    from alembic import command

    from scribble.db import _alembic_config

    eng = _fresh_engine()

    # Bring the DB to the revision just BEFORE the rename+index, where the table is still
    # `scribble_engagements` and there is no unique index — the shape a legacy DB is in.
    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), _BEFORE_RENAME)

    # Seed the outage: two boards linking the SAME core engagement.
    with eng.begin() as c:
        for bid, name in ((_KEEP, "TeamsPlus (first)"), (_DROP, "TeamsPlus (second)")):
            c.execute(
                text(
                    "INSERT INTO scribble_engagements "
                    "(id, name, scope_type, status, core_engagement_id, created_at, updated_at) "
                    "VALUES (:id, :name, 'external', 'in_progress', :core, now(), now())"
                ),
                {"id": bid, "name": name, "core": _CORE},
            )

    # Run the rename+dedup+index migration. Before the dedup fix this raised UniqueViolation and
    # aborted; now it must succeed.
    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), _RENAME_REV)

    insp = inspect(eng)
    names = set(insp.get_table_names())
    assert "scribble_report_boards" in names and "scribble_engagements" not in names
    assert _UQ in {i["name"] for i in insp.get_indexes("scribble_report_boards")}, (
        "the 1:1 partial-unique index must exist after the migration"
    )

    with eng.connect() as c:
        # Both boards survive — no data lost.
        total = c.execute(
            text("SELECT count(*) FROM scribble_report_boards WHERE id IN (:a, :b)"),
            {"a": _KEEP, "b": _DROP},
        ).scalar_one()
        assert total == 2, "dedup must keep BOTH boards (one linked, one orphaned), never delete"

        # Exactly one still links the core engagement, and it is the oldest (kept) board.
        linked = [str(x) for x in c.execute(
            text("SELECT id FROM scribble_report_boards WHERE core_engagement_id = :core"),
            {"core": _CORE},
        ).scalars().all()]
        assert linked == [_KEEP], f"oldest board must keep the link; got {linked}"

        dropped_link = c.execute(
            text("SELECT core_engagement_id FROM scribble_report_boards WHERE id = :b"),
            {"b": _DROP},
        ).scalar_one()
        assert dropped_link is None, "the newer duplicate's link must be nulled, not its row"
