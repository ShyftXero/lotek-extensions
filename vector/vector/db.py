"""SQLAlchemy base + session helpers.

Vector owns its own declarative ``Base``. When mounted in a host (lotek), ``register()`` is handed the
host engine/session factory and simply creates Vector's tables in the shared database; the tables are
``vector_``-prefixed so they never collide with host tables.

``create_all`` additionally runs a minimal, additive, idempotent migration (ADD COLUMN + CREATE INDEX IF
NOT EXISTS) so a model that gains a nullable/defaulted column keeps working on a database that already
had the table. This mirrors Scribble's ``create_all`` and lotek's ``migrate_sqlite``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_all(engine) -> None:
    """Create Vector's tables, then additively add any new columns (and their indexes) to pre-existing
    tables.

    SQLAlchemy's ``create_all`` only ever creates MISSING TABLES — never new COLUMNS on a table that
    already exists. Vector has no migration framework, so when a model gains a column a database that
    already had the table would be left without it and the next write would fail. This runs a minimal,
    additive, idempotent migration: for each table that ALREADY existed before ``create_all``,
    ``ALTER TABLE ADD COLUMN`` for any model column the DB is missing, then create any declared index on
    a newly added column.

    Additive-only, never drops or renames, safe to run on every mount. Only NULLABLE or DEFAULTED columns
    are added (a NOT-NULL-no-default column can't be added to populated rows, so it's skipped). Only
    INDEXes are retrofitted — a UNIQUE/CHECK on a column added this way is not enforced on upgraded DBs.
    """
    from sqlalchemy import inspect, text

    from vector import models  # noqa: F401  (import for side effect: register the mapped tables)

    pre_existing = set(inspect(engine).get_table_names())  # capture BEFORE create_all
    Base.metadata.create_all(engine)  # creates missing tables at full shape; no-ops existing ones

    insp = inspect(engine)  # fresh inspector reflecting the post-create_all schema
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in pre_existing:
                continue  # freshly created by create_all above — already has all columns + indexes
            have = {c["name"] for c in insp.get_columns(table.name)}
            added = False
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable and col.default is None and col.server_default is None:
                    continue  # can't safely add a NOT NULL column with no default to existing rows
                coltype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))
                have.add(col.name)
                added = True
            if added:
                for index in table.indexes:
                    if all(c.name in have for c in index.columns):
                        index.create(bind=conn, checkfirst=True)
