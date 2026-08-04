"""SQLAlchemy base + session helpers.

Scribble owns its own declarative ``Base`` when standalone. When mounted in Lotek, ``register()`` is
handed the host engine/session factory and simply creates Scribble's tables in the shared database; the
model *shapes* (not the Base identity) are what align with Lotek. Reconciling to a single ``Base`` is a
port-checkpoint concern, deliberately out of scope here.
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


#: Table-name suffixes Scribble owns (real names are ``scribble_<suffix>``). Kept in sync with
#: ``models.py`` __tablename__s; used only by the one-shot fraction->scribble rename below.
_TABLE_SUFFIXES = (
    "clients", "engagements", "assessment_types", "finding_groups", "vuln_templates",
    "vuln_map", "findings", "artifacts", "variables", "variable_values", "tags",
    "finding_tags", "template_tags", "report_templates", "report_renders", "collab_docs",
    "checklist_templates", "checklist_template_items", "engagement_checklists",
    "engagement_checklist_items",
)


def _rename_from_fraction(engine) -> None:
    """One-shot, idempotent, data-preserving rename of this extension's tables from the old
    ``fraction_`` prefix to ``scribble_`` (the extension was renamed Fraction -> Scribble).

    Runs at the TOP of ``create_all`` — BEFORE ``Base.metadata.create_all`` — so it renames the
    POPULATED tables in place rather than letting create_all build empty ``scribble_`` tables beside
    the old data. Guarded per table (rename only when the old name exists and the new one does not), so
    it is a no-op on a fresh database and on every boot after the first. On Postgres — the real backend
    — ``RENAME TO`` carries foreign-key constraints across automatically; index/sequence names keep
    their old ``fraction_``-prefixed labels (cosmetic, still functional). The core ``jobs`` table's
    ``promoted_extension`` value ('fraction' -> 'scribble') is the host's to migrate, not this module's.
    """
    from sqlalchemy import inspect, text

    existing = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for suffix in _TABLE_SUFFIXES:
            old, new = f"fraction_{suffix}", f"scribble_{suffix}"
            if old in existing and new not in existing:
                conn.execute(text(f'ALTER TABLE "{old}" RENAME TO "{new}"'))


def create_all(engine) -> None:
    """Create Scribble's tables, then additively add any new columns (and their indexes) to
    pre-existing tables.

    SQLAlchemy's ``create_all`` only ever creates MISSING TABLES — never new COLUMNS on a table that
    already exists, and it skips a pre-existing table entirely (so it won't add a new index either).
    Scribble has no migration framework, so when a model gains a column (e.g. ``Engagement.owner_id``) a
    database that already had the table would be left without it and the next write would fail. This runs
    a minimal, additive, idempotent migration: for each table that ALREADY existed before ``create_all``,
    ``ALTER TABLE ADD COLUMN`` for any model column the DB is missing, then create any declared index on a
    newly added column (mirrors lotek's ``migrate_sqlite``, which pairs ADD COLUMN with CREATE INDEX IF
    NOT EXISTS).

    Scope/limits — additive-only, never drops or renames, safe to run on every mount:
      * Columns: only NULLABLE or DEFAULTED columns are added (a NOT-NULL-no-default column can't be added
        to populated rows, so it's skipped).
      * Constraints: only INDEXes are retrofitted. A UNIQUE/CHECK constraint on a column added this way is
        NOT enforced on upgraded DBs — a column that needs one must be handled explicitly (e.g. a bespoke
        migration), not relied upon through this path.
    """
    from sqlalchemy import inspect, text

    from scribble import models  # noqa: F401

    _rename_from_fraction(engine)  # migrate an old fraction_*-prefixed DB in place, before create_all
    pre_existing = set(inspect(engine).get_table_names())  # capture BEFORE create_all (post-rename)
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
                # create_all skipped this pre-existing table, so an index on a just-added column
                # (e.g. ix_scribble_engagements_owner_id) is missing. checkfirst -> IF NOT EXISTS.
                for index in table.indexes:
                    if all(c.name in have for c in index.columns):
                        index.create(bind=conn, checkfirst=True)
