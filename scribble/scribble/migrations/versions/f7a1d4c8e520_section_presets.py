"""scribble_section_presets: operator-saved section arrangements (#Q10)

Org-wide saved section orders (order + on/off), offered in the composer's preset combobox. ``fingerprint``
(a canonical hash of the ordered (key, enabled) sequence) is UNIQUE so two operators cannot save the
identical arrangement twice. A fresh create_all database is built from the models and stamped at head, so
this only creates the table on a PRE-EXISTING deployment. Idempotent (guards on the reflected table).

Revision ID: f7a1d4c8e520
Revises: e6c9f4a2b83d
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a1d4c8e520"
down_revision: str | tuple[str, ...] | None = "e6c9f4a2b83d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_section_presets"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("specs", sa.JSON(), nullable=False),
            sa.Column("fingerprint", sa.String(length=64), nullable=False),
            sa.Column("created_by", sa.String(length=120), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("fingerprint", name="uq_scribble_section_presets_fingerprint"),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table(_TABLE):
        op.drop_table(_TABLE)
