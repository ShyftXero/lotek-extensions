"""The UUIDv7 PK migration must move DATA, not just types (lotek#335).

A migration that produces the right schema and the wrong rows passes every type check and loses the
customer's report. So these tests assert on relationships surviving — a finding still attached to *its*
engagement — rather than on column types, which are the easy half.

Postgres-gated: the migration is written in Postgres DDL and the whole point of the exercise is the
backend that enforces referential integrity.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

PG_URL = os.environ.get("SCRIBBLE_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs a real Postgres (SCRIBBLE_TEST_PG_URL)")

MAP_TABLE = "scribble_pk_migration_map"
BASELINE = "e17599b0880a"


def _wiped_engine():
    from scribble.db import Base

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    with eng.begin() as c:
        for t in (MAP_TABLE, "scribble_alembic_version", "scribble_clients_pre_mount_remap"):
            c.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))
    return eng


def _build_int_schema_with_data(eng):
    """Stand up the PRE-migration world: integer PKs, populated, stamped at the baseline.

    Built by running the baseline revision itself rather than by hand, so the fixture cannot drift away
    from the schema the migration will actually meet in production.
    """
    from alembic import command

    from scribble.db import _alembic_config

    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), BASELINE)

    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO scribble_engagements "
            "(id, name, scope_type, status, created_at, updated_at) VALUES "
            "(1, 'Acme Q3', 'external', 'active', now(), now()), "
            "(2, 'Beta Corp', 'external', 'active', now(), now())"
        ))
        c.execute(text(
            "INSERT INTO scribble_findings (id, engagement_id, title, severity, confidence, "
            "status, content_json, content_html, order_index, include_in_report, created_at, "
            "updated_at) VALUES "
            "(10, 1, 'SQLi in login', 'high', 'medium', 'new', '{}', '{}', 0, true, now(), now()), "
            "(11, 2, 'XSS in search', 'medium', 'medium', 'new', '{}', '{}', 0, true, now(), now())"
        ))


def test_migration_preserves_every_row_and_its_relationships():
    from alembic import command

    from scribble.db import _alembic_config

    eng = _wiped_engine()
    _build_int_schema_with_data(eng)

    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), "head")

    with eng.connect() as c:
        # Types actually changed...
        kinds = {
            col["name"]: type(col["type"]).__name__
            for col in inspect(eng).get_columns("scribble_engagements")
        }
        assert kinds["id"] == "UUID", f"engagement PK is still {kinds['id']}"

        # ...and, the part that matters, each finding is still attached to ITS OWN engagement.
        pairs = c.execute(text(
            "SELECT f.title, e.name FROM scribble_findings f "
            "JOIN scribble_engagements e ON f.engagement_id = e.id ORDER BY f.title"
        )).fetchall()
    assert [tuple(r) for r in pairs] == [("SQLi in login", "Acme Q3"), ("XSS in search", "Beta Corp")], (
        "findings were re-pointed at the wrong engagements — the FK backfill joined incorrectly"
    )


def test_the_mapping_table_survives_the_migration_and_covers_the_cross_repo_reference():
    """`jobs.promoted_ref_id` (in CORE) holds Scribble engagement integers. With no `legacy_id` column,
    this mapping table is the ONLY record of old->new — so it must exist after the upgrade, and it must
    contain the engagement rows lotek's revision will look up."""
    from alembic import command

    from scribble.db import _alembic_config

    eng = _wiped_engine()
    _build_int_schema_with_data(eng)
    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), "head")

    with eng.connect() as c:
        assert MAP_TABLE in set(inspect(eng).get_table_names()), (
            "the mapping table was dropped by the migration that created it — the job->report link is "
            "now unreconstructable"
        )
        rows = c.execute(text(
            f"SELECT old_int_id, new_uuid FROM {MAP_TABLE} "
            "WHERE table_name = 'scribble_engagements' ORDER BY old_int_id"
        )).fetchall()
        assert [r[0] for r in rows] == [1, 2], "engagement ids missing from the mapping"

        # The mapped uuid is the one the row actually carries — not a fresh, unrelated uuid.
        for old_id, mapped in rows:
            name = c.execute(
                text("SELECT name FROM scribble_engagements WHERE id = :u"), {"u": mapped}
            ).scalar_one()
            assert name == {1: "Acme Q3", 2: "Beta Corp"}[old_id]


def test_new_rows_get_uuid7_not_uuid4():
    """v7 is load-bearing: `ORDER BY id` recovers creation order, which is why core chose it. A v4
    default would silently cost that (and it is invisible without checking the version nibble)."""
    from alembic import command

    from scribble.db import _alembic_config, make_session_factory
    from scribble.models import Engagement

    eng = _wiped_engine()
    _build_int_schema_with_data(eng)
    with eng.begin() as conn:
        command.upgrade(_alembic_config(conn), "head")

    with make_session_factory(eng)() as db:
        e = Engagement(name="post-migration")
        db.add(e)
        db.commit()
        assert isinstance(e.id, uuid.UUID)
        assert e.id.version == 7, f"expected a UUIDv7, got v{e.id.version}"


def test_downgrade_refuses_rather_than_inventing_ids():
    """Reversing would have to mint integers that never existed, silently repointing every external
    reference at the wrong rows. Refusing loudly is the honest behaviour."""
    from scribble.migrations.versions import b1d4a7c9e250_uuidv7_primary_keys as rev

    with pytest.raises(NotImplementedError, match="irreversible"):
        rev.downgrade()
