"""A report board is 1:1 with a CORE engagement, on a database the product actually builds (ext#259).

The partial-unique `uq_scribble_report_board_core_engagement` exists to stop two boards on one core
engagement -- the lotek#914 prod outage. It was declared ONLY in alembic (d1e2f3a4b5c6), so it was
absent on every fresh deployment and every unit test: `db.run_migrations` builds a fresh schema with
`Base.metadata.create_all` and stamps head WITHOUT replaying the chain, so a migration-only index never
arrives. Mirror of `test_promote_job_concurrent_dedup.py`'s fresh-database check for the sibling index.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

import scribble.models as fm

_UQ = "uq_scribble_report_board_core_engagement"


def test_a_fresh_database_carries_the_core_engagement_unique_index(session_factory):
    with session_factory() as db:
        names = {i["name"] for i in inspect(db.get_bind()).get_indexes("scribble_report_boards")}
    assert _UQ in names, f"{_UQ} must exist on a database the product builds; got {sorted(names)}"


def test_the_index_refuses_two_boards_on_one_core_engagement(session_factory):
    """The constraint bites at the DB level -- a unique, non-unique, or missing WHERE all fail here."""
    core_id = uuid.uuid7()
    with session_factory() as db:
        db.add(fm.ReportBoard(name="a", scope_type="internal", core_engagement_id=core_id))
        db.commit()
        db.add(fm.ReportBoard(name="a (dup)", scope_type="internal", core_engagement_id=core_id))
        with pytest.raises(IntegrityError):
            db.commit()


def test_null_core_engagement_ids_are_exempt(session_factory):
    """Partial on non-null: standalone boards carry NULL and many share a database."""
    with session_factory() as db:
        db.add(fm.ReportBoard(name="s1", scope_type="internal"))
        db.add(fm.ReportBoard(name="s2", scope_type="internal"))
        db.commit()  # two NULLs must not collide
