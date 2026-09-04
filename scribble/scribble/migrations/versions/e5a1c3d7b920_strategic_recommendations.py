"""lotek#623: Engagement.strategic_recommendations

One nullable JSON column on ``scribble_engagements`` backing the authored Strategic Recommendations
report section (lotek#623) — an ordered list of plain-text items. Nullable, so no cross-DB
``server_default`` cast is needed; every read coerces NULL to ``[]`` via
``models.normalize_strategic_recommendations``.

Idempotent, matching the chain's house style: a fresh ``scribble.db.create_all`` DB is stamped at head
without ever running this, and the adoption path may ALTER-ADD ahead of it, so guard on column presence
rather than assuming the column is absent.

Continues the SINGLE head (``d3f5a7c9b1e2`` attack-chains). A fork would silently break scribble's mount
(ext#169) — ``tests/test_migration_single_head.py`` guards it.

Revision ID: e5a1c3d7b920
Revises: d3f5a7c9b1e2
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a1c3d7b920"
down_revision: str | None = "d3f5a7c9b1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_engagements"
_COLUMN = "strategic_recommendations"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if _COLUMN not in cols:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if _COLUMN in cols:
        op.drop_column(_TABLE, _COLUMN)
