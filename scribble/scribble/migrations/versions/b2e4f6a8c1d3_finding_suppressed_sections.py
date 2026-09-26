"""BoardFinding.suppressed_sections: per-section report suppression (report composition control)

Adds one additive, nullable JSON column to ``scribble_findings`` holding the list of section keys the
operator has chosen to OMIT from the rendered deliverable for a finding (content blocks + the derived
affected-assets/evidence/references sections). Honored once in ``build_report_context`` (the data is made
absent), so every renderer skips it by the same path it already uses for an empty field.

Additive + nullable, safe on a populated table: a row created before this column reads NULL and every read
uses ``finding.suppressed_sections or []``. A fresh database never runs this — it is built from the models
(which already declare the column) and stamped at head; this only backfills a pre-existing deployment.
Idempotent (guards on the reflected columns).

Revision ID: b2e4f6a8c1d3
Revises: d1e2f3a4b5c6
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2e4f6a8c1d3"
down_revision: str | tuple[str, ...] | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_findings"
_COLUMN = "suppressed_sections"


def _present() -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _present():
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    if _COLUMN in _present():
        op.drop_column(_TABLE, _COLUMN)
