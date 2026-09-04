"""lotek#620: Engagement.risk_override + risk_override_rationale

Two nullable columns on ``scribble_engagements`` backing the manual override of a report's COMPUTED
overall risk band (lotek#620). ``risk_override`` reuses the existing ``severity`` Postgres enum type
(already CREATEd by the baseline revision for ``scribble_findings.severity``), so the ADD COLUMN must
NOT re-create it -- ``create_type=False``, else ``upgrade head`` fails with
``type "severity" already exists``.

Idempotent, matching the chain's house style: a fresh ``scribble.db.create_all`` DB is stamped at head
without ever running this, and the adoption path may ALTER-ADD ahead of it, so guard on column presence
rather than assuming the columns are absent.

Revision ID: f0a1c2d3e4b5
Revises: 76a1de5a7c83
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1c2d3e4b5"
down_revision: str | None = "76a1de5a7c83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_engagements"
# create_type=False: the ``severity`` enum TYPE already exists (baseline revision); do not re-CREATE it.
_SEVERITY = sa.Enum("info", "low", "medium", "high", "critical", name="severity", create_type=False)


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "risk_override" not in cols:
        op.add_column(_TABLE, sa.Column("risk_override", _SEVERITY, nullable=True))
    if "risk_override_rationale" not in cols:
        op.add_column(_TABLE, sa.Column("risk_override_rationale", sa.Text(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    if "risk_override_rationale" in cols:
        op.drop_column(_TABLE, "risk_override_rationale")
    if "risk_override" in cols:
        op.drop_column(_TABLE, "risk_override")
