"""ReportBoard.methodology_text + scope_limitations_text: editable standing-prose overrides

Two additive, nullable Text columns (#Q4). NULL = the generated standing text (methodology phases / the
standing scope-and-limitations statement); a value replaces it for that report. An existing row reads
byte-identical to before. A fresh create_all database is built from the models and stamped at head, so this
only backfills a PRE-EXISTING deployment. Idempotent (guards on the reflected columns).

Revision ID: e6c9f4a2b83d
Revises: d5b8e3a1c62f
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6c9f4a2b83d"
down_revision: str | tuple[str, ...] | None = "d5b8e3a1c62f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_report_boards"
_COLUMNS = ("methodology_text", "scope_limitations_text")


def _present() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    have = _present()
    for col in _COLUMNS:
        if col not in have:
            op.add_column(_TABLE, sa.Column(col, sa.Text(), nullable=True))


def downgrade() -> None:
    have = _present()
    for col in reversed(_COLUMNS):
        if col in have:
            op.drop_column(_TABLE, col)
