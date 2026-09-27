"""The ext#257 migration (`a1b7c3e05d94`) must self-heal duplicate promoted findings BEFORE it builds
the partial-unique index — otherwise an existing database that the race already doubled aborts the whole
migration, and scribble fails to MOUNT. That is the lotek#914 shape `d1e2f3a4b5c6` was taught: a
constraint that refuses to install protects nothing.

The two duplicate branches are the whole content of the migration, and they differ in what an operator
stands to lose:

  * an UNTOUCHED duplicate (nothing references it) is deleted — its existence IS the defect, and removing
    it restores exactly the state a sequential promote would have produced;
  * a REFERENCED duplicate (a variable value, a prose override, a child finding — somebody edited this
    copy) survives with its `source_finding_id` nulled and `include_in_report` cleared, so the index can
    build, the deliverable stops showing the duplicate, and no operator work is destroyed.

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
_REV = "a1b7c3e05d94"
_BEFORE = "f7a1d4c8e520"  # down_revision of the dedup+index migration
_UQ = "uq_scribble_findings_engagement_source"

_BOARD = "01a10000-0000-7000-8000-0000000000bb"
_SOURCE = "01a10000-0000-7000-8000-0000000000cc"
_KEEP = "01a10000-0000-7000-8000-000000000001"  # smallest id -> oldest -> keeps the link
_UNTOUCHED = "01a10000-0000-7000-8000-000000000002"  # nothing references it -> deleted
_EDITED = "01a10000-0000-7000-8000-000000000003"  # a variable value hangs off it -> kept, unlinked


def _fresh_engine():
    from scribble.db import Base

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {VERSION_TABLE}"))
        c.execute(text("DROP TABLE IF EXISTS scribble_pk_migration_map CASCADE"))
    return eng


def test_the_migration_resolves_duplicate_promotions_then_builds_the_unique_index():
    from alembic import command

    from scribble.db import _alembic_config

    eng = _fresh_engine()
    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), _BEFORE)

    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO scribble_report_boards (id, name, scope_type, status, created_at, updated_at) "
                "VALUES (:id, 'ext257', 'internal', 'in_progress', now(), now())"
            ),
            {"id": _BOARD},
        )
        # The race: three rows, one scan finding, one board.
        for fid, title in (
            (_KEEP, "Doubled finding"),
            (_UNTOUCHED, "Doubled finding"),
            (_EDITED, "Doubled finding"),
        ):
            c.execute(
                text(
                    "INSERT INTO scribble_findings "
                    "(id, engagement_id, source_finding_id, title, severity, confidence, status, "
                    " content_json, content_html, order_index, include_in_report, created_at, updated_at) "
                    "VALUES (:id, :eng, :src, :title, 'medium', 'medium', 'new', '{}', '{}', 0, true, "
                    "        now(), now())"
                ),
                {"id": fid, "eng": _BOARD, "src": _SOURCE, "title": title},
            )
        # An operator invested work in ONE of the duplicates.
        c.execute(
            text(
                "INSERT INTO scribble_variables "
                "(id, key, label, scope, value_type, options, builtin, created_at, updated_at) "
                "VALUES (:id, 'EXT257', 'ext257', 'finding', 'str_', '[]', false, now(), now())"
            ),
            {"id": "01a10000-0000-7000-8000-0000000000dd"},
        )
        c.execute(
            text(
                "INSERT INTO scribble_variable_values "
                "(id, variable_id, finding_id, value, created_at, updated_at) "
                "VALUES (:id, :var, :fid, 'operator edit', now(), now())"
            ),
            {
                "id": "01a10000-0000-7000-8000-0000000000ee",
                "var": "01a10000-0000-7000-8000-0000000000dd",
                "fid": _EDITED,
            },
        )

    # Before the dedup this raised UniqueViolation and aborted the mount; now it must succeed.
    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), _REV)

    assert _UQ in {i["name"] for i in inspect(eng).get_indexes("scribble_findings")}, (
        "the partial-unique index must exist after the migration"
    )

    with eng.connect() as c:
        linked = [
            str(x)
            for x in c.execute(
                text("SELECT id FROM scribble_findings WHERE source_finding_id = :s"), {"s": _SOURCE}
            ).scalars().all()
        ]
        assert linked == [_KEEP], f"the oldest promoted row must keep the link; got {linked}"

        gone = c.execute(
            text("SELECT count(*) FROM scribble_findings WHERE id = :id"), {"id": _UNTOUCHED}
        ).scalar_one()
        assert gone == 0, "an untouched duplicate is machine output the race created — delete it"

        kept = c.execute(
            text(
                "SELECT source_finding_id, include_in_report FROM scribble_findings WHERE id = :id"
            ),
            {"id": _EDITED},
        ).first()
        assert kept is not None, "a duplicate an operator edited must never be deleted"
        assert kept[0] is None, "its link must be nulled so the index can build"
        assert kept[1] is False, "and it must drop out of the deliverable, which is the defect"

        # The operator's own row survives intact — deleting the finding would have taken it along.
        survived = c.execute(
            text("SELECT value FROM scribble_variable_values WHERE finding_id = :id"), {"id": _EDITED}
        ).scalar_one()
        assert survived == "operator edit"


def test_the_migration_is_idempotent_and_leaves_clean_databases_alone():
    """It runs on every mount of an existing deployment. A second pass must not touch a healthy board."""
    from alembic import command

    from scribble.db import _alembic_config

    eng = _fresh_engine()
    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), _BEFORE)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO scribble_report_boards (id, name, scope_type, status, created_at, updated_at) "
                "VALUES (:id, 'ext257 clean', 'internal', 'in_progress', now(), now())"
            ),
            {"id": _BOARD},
        )
        c.execute(
            text(
                "INSERT INTO scribble_findings "
                "(id, engagement_id, source_finding_id, title, severity, confidence, status, "
                " content_json, content_html, order_index, include_in_report, created_at, updated_at) "
                "VALUES (:id, :eng, :src, 'Single', 'medium', 'medium', 'new', '{}', '{}', 0, true, "
                "        now(), now())"
            ),
            {"id": _KEEP, "eng": _BOARD, "src": _SOURCE},
        )

    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), _REV)
    with eng.begin() as conn:  # downgrade + re-upgrade exercises the guarded second pass
        command.downgrade(_alembic_config(conn), _BEFORE)
        command.upgrade(_alembic_config(conn), _REV)

    with eng.connect() as c:
        row = c.execute(
            text("SELECT source_finding_id, include_in_report FROM scribble_findings WHERE id = :id"),
            {"id": _KEEP},
        ).first()
    assert row is not None and str(row[0]) == _SOURCE and row[1] is True
