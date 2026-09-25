"""ReportBoard.section_order: per-report section composition (order + on/off)

One additive, nullable JSON column holding an ordered list ``[{"key": <block>, "enabled": bool}, ...]``
(keys = reporting.layouts.BLOCK_KEYS). NULL = the default composition, so an existing row reads
byte-identical to before this column existed. A fresh database is built from the models and stamped at
head, so this only backfills a pre-existing deployment. Idempotent (guards on the reflected column).

Revision ID: f4a7c2e91b60
Revises: c3f8b1a4d206
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a7c2e91b60"
down_revision: str | tuple[str, ...] | None = "c3f8b1a4d206"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_report_boards"
_COLUMN = "section_order"


def _present() -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _present():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    if _COLUMN in _present():
        op.drop_column(_TABLE, _COLUMN)
