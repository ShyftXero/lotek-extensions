"""scribble_findings.target_url must be unbounded TEXT (lotek-extensions#402).

A promoted finding's URL is evidence, and a scanner can report one that exceeds 1024 characters: dalfox
writing a crawl result into ``matched_at`` produced a comma-joined pair that overflowed the old
``VARCHAR(1024)``. Postgres refused the promotion UPDATE with ``StringDataRightTruncation`` and the whole
"Adopt job" action returned 500. SQLite does not enforce the length, so every unit test stayed green.

The hermetic half guards the model declaration. The database half proves the migration repairs a
deployment that still carries the bounded column.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import String, Text, create_engine, text

PG_URL = os.environ.get("SCRIBBLE_TEST_PG_URL")


def test_target_url_column_is_declared_unbounded_text():
    """Red while the column was ``String(1024)``, green as ``Text``. Needs no database."""
    from scribble.models import BoardFinding

    col_type = BoardFinding.__table__.c.target_url.type
    assert isinstance(col_type, Text), f"target_url is {col_type!r}, must be Text (unbounded)"
    assert getattr(col_type, "length", None) is None


@pytest.mark.skipif(not PG_URL, reason="needs a real Postgres (SCRIBBLE_TEST_PG_URL)")
def test_the_migration_widens_a_pre_existing_varchar1024_target_url():
    """The prod path, unprovable on SQLite: a deployment stamped at the previous head carries
    ``VARCHAR(1024)``; the next mount runs this revision and widens it. Red first (a long URL is
    refused), then green through the upgrade."""
    from alembic import command
    from sqlalchemy.exc import DataError

    from scribble.db import Base, _alembic_config, make_session_factory, run_migrations
    from scribble.models import BoardFinding, ReportBoard

    _PREV_HEAD = "c7d9f1b3e5a2"
    long_url = "https://example.test/" + "a" * 1200

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS scribble_alembic_version"))

    run_migrations(eng)  # fresh path: built at TEXT and stamped at head

    with eng.begin() as c:
        c.execute(text("ALTER TABLE scribble_findings ALTER COLUMN target_url TYPE VARCHAR(1024)"))
        command.stamp(_alembic_config(c), _PREV_HEAD)

    session_factory = make_session_factory(eng)
    with session_factory() as db:
        db.add(ReportBoard(name="E"))
        db.commit()
        eid = db.query(ReportBoard).one().id

    with session_factory() as db, pytest.raises(DataError, match="too long"):
        db.add(BoardFinding(engagement_id=eid, title="t", severity="info", target_url=long_url))
        db.commit()

    run_migrations(eng)  # the repair: c7d9f1b3e5a2 -> e9c4a2b7f310 widens the column

    with session_factory() as db:
        db.add(BoardFinding(engagement_id=eid, title="t2", severity="info", target_url=long_url))
        db.commit()
        stored = db.query(BoardFinding).filter_by(title="t2").one().target_url
    assert stored == long_url


def test_a_long_finding_title_is_not_bounded_by_a_varchar():
    """The title arrives from the host finding (1024 there) and used to be VARCHAR(512) here, so a long
    scanner title aborted the promotion exactly like the URL did (#402)."""
    from scribble.models import BoardFinding

    col_type = BoardFinding.__table__.c.title.type
    assert isinstance(col_type, Text), f"title is {col_type!r}, must be Text (unbounded)"
    assert getattr(col_type, "length", None) is None


def test_caption_is_clipped_to_its_column():
    """A caption is a display label: it must fit a bounded caption column if one is ever reintroduced.

    ``Artifact.caption`` is Text today (baseline migration e17599b0880a), so this guards the formatter,
    not the current schema. The string matches what promote.py emits.
    """
    from scribble.promote import CAPTION_MAX, _caption

    long_url = "https://example.test/" + "b" * 400
    clipped = _caption(f"Scan screenshot: {long_url}")
    assert len(clipped) == CAPTION_MAX
    assert clipped.endswith("\u2026")
    assert _caption("short") == "short"
