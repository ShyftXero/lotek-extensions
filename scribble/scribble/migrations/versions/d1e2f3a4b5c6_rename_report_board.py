"""Report Board rework: rename scribble_engagements -> scribble_report_boards + 1:1 partial-unique

The scribble "engagement" model is renamed ``ReportBoard`` (lotek Report Board rework,
plans/feat-scribble-report-board-rework.md). Physical table ``scribble_engagements`` ->
``scribble_report_boards``, plus a PARTIAL UNIQUE index on ``core_engagement_id`` (non-null values)
enforcing the 1:1 report-board <-> core-engagement rule. The column stays nullable so legacy orphan
boards survive the migration; the UI only ever creates a linked board.

Idempotent + guarded, matching the chain's house style: a fresh ``scribble.db.create_all`` DB is stamped
at head (it builds ``scribble_report_boards`` straight from the model) and never runs this. On an existing
DB with duplicate non-null ``core_engagement_id`` values the unique index creation will fail loudly --
resolve the duplicates first (the by-core resolver already collapses to the oldest board).

Revision ID: d1e2f3a4b5c6
Revises: c9e1a2b3d4f5
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "c9e1a2b3d4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "scribble_engagements"
_NEW = "scribble_report_boards"
_UQ = "uq_scribble_report_board_core_engagement"


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if _OLD in tables and _NEW not in tables:
        op.rename_table(_OLD, _NEW)
    tables = set(sa.inspect(bind).get_table_names())
    if _NEW in tables:
        idx = {i["name"] for i in sa.inspect(bind).get_indexes(_NEW)}
        if _UQ not in idx:
            op.create_index(
                _UQ, _NEW, ["core_engagement_id"], unique=True,
                postgresql_where=sa.text("core_engagement_id IS NOT NULL"),
                sqlite_where=sa.text("core_engagement_id IS NOT NULL"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if _NEW in tables:
        idx = {i["name"] for i in sa.inspect(bind).get_indexes(_NEW)}
        if _UQ in idx:
            op.drop_index(_UQ, table_name=_NEW)
        if _OLD not in tables:
            op.rename_table(_NEW, _OLD)
