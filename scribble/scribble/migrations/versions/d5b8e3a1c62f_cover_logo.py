"""Cover logo library: scribble_report_logos + ReportBoard.cover_logo_id

The org-wide cover-logo library (#Q3): upload once, pick per report. A logo is not engagement-scoped, so
its bytes live inline in this table rather than in the object store (INV-OBJSTORE-01 anchors object-store
blobs to a core engagement). ``ReportBoard.cover_logo_id`` NULL => the stock lotek mark.

Continues the SINGLE head. A fresh ``create_all`` database is built from the models and stamped at head
without running this, so it only backfills a PRE-EXISTING deployment. Idempotent: guards on the reflected
table/column.

Revision ID: d5b8e3a1c62f
Revises: f4a7c2e91b60
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5b8e3a1c62f"
down_revision: str | tuple[str, ...] | None = "f4a7c2e91b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOGOS = "scribble_report_logos"
_BOARDS = "scribble_report_boards"
_COLUMN = "cover_logo_id"
_FK = "fk_scribble_report_boards_cover_logo_id"


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(_LOGOS):
        op.create_table(
            _LOGOS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("label", sa.String(length=120), nullable=False),
            sa.Column("content_type", sa.String(length=80), nullable=False),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.Column("created_by", sa.String(length=120), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if _COLUMN not in _columns(_BOARDS):
        op.add_column(_BOARDS, sa.Column(_COLUMN, sa.Uuid(), nullable=True))
        op.create_foreign_key(_FK, _BOARDS, _LOGOS, [_COLUMN], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if _COLUMN in _columns(_BOARDS):
        op.drop_constraint(_FK, _BOARDS, type_="foreignkey")
        op.drop_column(_BOARDS, _COLUMN)
    if insp.has_table(_LOGOS):
        op.drop_table(_LOGOS)
