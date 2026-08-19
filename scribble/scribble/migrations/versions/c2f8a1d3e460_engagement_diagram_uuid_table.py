"""Post-merge reconciliation: engagement_diagram UUID table + the create_all-only core_engagement_id column

Two schema elements landed on ``main`` via ``scribble.db.create_all`` alone, without ever entering the
Alembic chain, and the UUIDv7 migration (``b1d4a7c9e250``) predates both. A database brought to head by
Alembic ONLY (no create_all pass — e.g. a pure migration test, or a fresh prod that runs migrations) was
therefore left without them. This revision folds both into the chain:

1. **scribble_engagement_diagram** (ext#48 / PR #72) — authored while ids were still sequential integers
   and shipped with no migration. After ``b1d4a7c9e250`` made every other PK UUIDv7 it was left with an
   ``Integer`` PK and an ``Integer`` ``engagement_id`` FK pointing at the now-``uuid``
   ``scribble_engagements.id`` — a mixed-type FK Postgres refuses. This creates it (or converts an empty
   int-shaped one) as UUID.
2. **scribble_engagements.core_engagement_id** (ext#49 / PR #66) — a ``SoftHostId`` column added to the
   model but never to a migration; an Alembic-only DB lacked it and every ORM write to the table failed
   with ``UndefinedColumn``. Added here, idempotently.

Because it never existed in a prior migration, three prod states are possible and all are handled:
  * the table is absent            -> create it fresh with UUID columns;
  * it exists with UUID ``id``     -> nothing to do (a fresh create_all already built it correctly);
  * it exists with an INTEGER ``id`` (a create_all deploy that predates this) -> drop + recreate as UUID,
    but ONLY when empty. A non-empty int-keyed table means real linked diagrams exist and would be lost;
    we refuse loudly rather than silently drop them (this is a brand-new feature table, expected empty).

Revision ID: c2f8a1d3e460
Revises: b1d4a7c9e250
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import scribble.db  # noqa: F401 -- referenced by name for SoftHostId, matching the baseline revision

revision: str = "c2f8a1d3e460"
down_revision: str | None = "b1d4a7c9e250"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_engagement_diagram"


def _create() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("engagement_id", sa.Uuid(), nullable=False),
        sa.Column("diagram_ref", sa.String(length=64), nullable=True),
        sa.Column("caption", sa.String(length=255), nullable=True),
        sa.Column("embed_html", sa.Text(), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("include_in_report", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["engagement_id"], ["scribble_engagements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f(f"ix_{_TABLE}_engagement_id"), _TABLE, ["engagement_id"], unique=False)


def _ensure_core_engagement_id() -> None:
    """Add scribble_engagements.core_engagement_id (ext#49) if the Alembic chain hasn't got it yet.

    Idempotent: the adoption path's ``_additive_column_sync`` may have already ALTER-ADDed it before this
    revision runs, and a fresh create_all DB is stamped at head without running this at all — so guard on
    the column's presence rather than assuming it is absent.
    """
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("scribble_engagements")}
    if "core_engagement_id" in cols:
        return
    op.add_column(
        "scribble_engagements",
        sa.Column("core_engagement_id", scribble.db.SoftHostId(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_scribble_engagements_core_engagement_id"),
        "scribble_engagements",
        ["core_engagement_id"],
        unique=False,
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    _ensure_core_engagement_id()

    if not insp.has_table(_TABLE):
        _create()
        return

    id_col = next((c for c in insp.get_columns(_TABLE) if c["name"] == "id"), None)
    already_uuid = id_col is not None and "uuid" in str(id_col["type"]).lower()
    if already_uuid:
        return

    row_count = bind.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one()
    if row_count:
        raise RuntimeError(
            f"{_TABLE} still has {row_count} integer-keyed row(s); this migration will not drop real "
            "attack-path diagrams. Port them to the UUID schema by hand, then re-run."
        )
    op.drop_table(_TABLE)
    _create()


def downgrade() -> None:
    # The pre-UUID shape was an int PK/FK, which cannot coexist with the now-UUID scribble_engagements.id
    # this table references. There is nothing coherent to downgrade TO once b1d4a7c9e250 is in place, so
    # dropping the table is the only reversible step (the feature is re-created on the next upgrade).
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        op.drop_table(_TABLE)
