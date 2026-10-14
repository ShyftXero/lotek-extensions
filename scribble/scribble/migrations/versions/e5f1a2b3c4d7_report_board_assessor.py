"""report board assessor override column

Adds ``scribble_report_boards.assessor`` (String, nullable): the OPERATOR OVERRIDE for a report's Assessor
line. Empty (the default) -> the renderer falls back to the derived display name(s) of the source-scan
owners ({{ASSESSOR}}, "Assessment Team" when several); set -> this wins, so the operator controls what a
client sees and freezes it against later job changes. Mirrors ``company_name``'s override-vs-derived.

Additive + nullable, safe on a populated table (every existing row reads NULL -> keeps deriving exactly as
before). A fresh DB is built from the models and stamped at head; this only backfills a PRE-EXISTING
deployment. Idempotent (guards on the reflected column). Continues the SINGLE head (``c8e1a4f7b230``); adds
no parallel head (a fork silently breaks the scribble mount - precedent ext#169). Reversible.

Revision ID: e5f1a2b3c4d7
Revises: c8e1a4f7b230
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f1a2b3c4d7"
down_revision: str | tuple[str, ...] | None = "c8e1a4f7b230"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TBL = "scribble_report_boards"


def _present() -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(_TBL)}


def upgrade() -> None:
    if "assessor" not in _present():
        op.add_column(_TBL, sa.Column("assessor", sa.String(length=255), nullable=True))


def downgrade() -> None:
    if "assessor" in _present():
        op.drop_column(_TBL, "assessor")
