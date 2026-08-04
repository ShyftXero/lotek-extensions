"""SQLAlchemy base + session helpers.

CREAM owns its own declarative ``Base``. When mounted in a host (lotek), ``register()`` is handed the
host engine/session factory and simply creates CREAM's tables in the shared database; the tables are
``cream_``-prefixed so they never collide with host tables.

``create_all`` additionally runs a minimal, additive, idempotent migration (ADD COLUMN + CREATE INDEX IF
NOT EXISTS) so a model that gains a nullable/defaulted column keeps working on a database that already
had the table. Mirrors Vector's ``create_all`` and lotek's ``migrate_sqlite``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class UuidPk:
    """v2 surrogate-PK convention: a UUIDv7, generated application-side (monotonic; ORDER BY id sorts by
    creation). ``uuid.uuid7`` is 3.14 stdlib; lotek v2 pins ``requires-python >= 3.14``. Not a core
    import — just stdlib + SQLAlchemy's dialect-agnostic ``Uuid`` (native 16-byte uuid on Postgres)."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_all(engine) -> None:
    """Create CREAM's tables, then additively add any new columns (and their indexes) to pre-existing
    tables. Additive-only, never drops/renames, safe on every mount. Only NULLABLE/DEFAULTED columns are
    added (a NOT-NULL-no-default column can't be added to populated rows, so it's skipped)."""
    from sqlalchemy import inspect, text

    from cream import models  # noqa: F401  (import for side effect: register the mapped tables)

    pre_existing = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in pre_existing:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            added = False
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable and col.default is None and col.server_default is None:
                    continue
                coltype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))
                have.add(col.name)
                added = True
            if added:
                for index in table.indexes:
                    if all(c.name in have for c in index.columns):
                        index.create(bind=conn, checkfirst=True)
