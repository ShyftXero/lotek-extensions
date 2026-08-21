"""scribble_artifacts.idempotency_key must be unbounded TEXT (#411 / lotek#411).

The upload dedup key is derived client-side as ``ev-<engagement>-<finding>-<basename>-<sha[:32]>``. Once
#372 moved engagement/finding PKs to 36-char UUIDs a routine key runs ~120 chars and overflows the old
``VARCHAR(80)`` — Postgres raises ``StringDataRightTruncation`` (a 500 on every evidence upload). SQLite
never enforced the length, so the whole unit suite stayed green while prod 500'd; the real proof therefore
needs Postgres.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import String, Text, create_engine, inspect, text

PG_URL = os.environ.get("SCRIBBLE_TEST_PG_URL")


def test_idempotency_key_column_is_declared_unbounded_text():
    """Hermetic model-contract guard: the declared type must be TEXT, never a bounded VARCHAR. Red while
    it was ``String(80)``, green as ``Text``. Catches a regression to any length-bounded type without
    needing a database at all."""
    from scribble.models import Artifact

    col_type = Artifact.__table__.c.idempotency_key.type
    assert isinstance(col_type, Text), f"idempotency_key is {col_type!r}, must be Text (unbounded)"
    assert getattr(col_type, "length", None) is None


@pytest.mark.skipif(not PG_URL, reason="needs a real Postgres (SCRIBBLE_TEST_PG_URL)")
def test_the_migration_widens_a_pre_existing_varchar80_idempotency_key():
    """The real prod path, unprovable on SQLite: a deployment stamped at the previous head carries a
    ``VARCHAR(80)`` column; the next mount runs this revision and widens it. Red first (a >80-char key is
    refused), then green through the upgrade.
    """
    from alembic import command
    from sqlalchemy.exc import DataError

    from scribble.db import Base, _alembic_config, make_session_factory, run_migrations
    from scribble.models import Artifact, Engagement

    _PREV_HEAD = "c2f8a1d3e460"
    long_key = "ev-" + "a" * 40 + "-" + "b" * 40 + "-shot.png-" + "c" * 32  # ~120 chars, well over 80

    eng = create_engine(PG_URL)
    Base.metadata.drop_all(eng)
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS scribble_alembic_version"))

    run_migrations(eng)  # build at current head (fresh path: create_all at TEXT + stamp head)

    # Rewind to the state a pre-#411 deployment actually carried: narrow the column AND move the Alembic
    # pointer back one revision, so the next run_migrations has a pending upgrade to apply.
    with eng.begin() as c:
        c.execute(text("ALTER TABLE scribble_artifacts ALTER COLUMN idempotency_key TYPE VARCHAR(80)"))
        command.stamp(_alembic_config(c), _PREV_HEAD)

    session_factory = make_session_factory(eng)
    with session_factory() as db:
        db.add(Engagement(name="E"))
        db.commit()
        eid = db.query(Engagement).one().id

    # RED: the long UUID-era key overflows VARCHAR(80).
    with session_factory() as db, pytest.raises(DataError, match="too long"):
        db.add(Artifact(engagement_id=eid, filename="shot.png", storage_path="x",
                        idempotency_key=long_key))
        db.commit()

    run_migrations(eng)  # the repair: c2f8a1d3e460 -> d7b3f1a4c680 widens the column

    col_type = {c["name"]: c["type"] for c in inspect(eng).get_columns("scribble_artifacts")}[
        "idempotency_key"
    ]
    assert isinstance(col_type, (String, Text)) and getattr(col_type, "length", None) is None

    # GREEN: the same key now lands.
    with session_factory() as db:
        db.add(Artifact(engagement_id=eid, filename="shot.png", storage_path="x",
                        idempotency_key=long_key))
        db.commit()
        assert db.query(Artifact).filter_by(idempotency_key=long_key).one()

    Base.metadata.drop_all(eng)
