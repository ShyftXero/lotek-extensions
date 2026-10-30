"""ext#248: attack-chain quality + copy-pasteable command appendix columns

Adds two additive, nullable columns to ``scribble_attack_chains`` so an IMPORTED exploiteer chain carries
the two things ext#248 asks for beyond the #628 narrative:

  * ``status`` (String(32)) - how the chain held up when an operator tried it, for THIS engagement
    (``enums.ChainQuality``: untested / valid / worked / partially_worked / failed). A plain VARCHAR, not
    an enum/CHECK, so it is safe to add on a populated table (the ``ReportBoard.status`` posture); NULL on
    a legacy row and read as ``untested``.
  * ``commands`` (Text) - exploiteer's numbered, copy-pastable reproduction block, stored byte-for-byte so
    the report can render a clean monospace appendix beside the prose steps.

Both additive + nullable, safe on a populated table: a chain created before these columns reads NULL, and
every read is NULL-tolerant (the renderer treats a NULL ``status`` as ``untested`` and a NULL/empty
``commands`` as "no appendix"). Reversible: ``downgrade`` drops the two columns.

A fresh database never runs this - it is built from the models (which already declare these columns) and
stamped at head; this only backfills a PRE-EXISTING deployment. Idempotent: guards on the reflected
columns. Continues the SINGLE head (``f3b8c1e07a92``); adds no parallel head (a fork silently breaks the
scribble mount - precedent ext#169).

Revision ID: c7d9f1b3e5a2
Revises: f3b8c1e07a92
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d9f1b3e5a2"
down_revision: str | tuple[str, ...] | None = "f3b8c1e07a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAINS = "scribble_attack_chains"


def _present(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    cols = _present(_CHAINS)
    if "status" not in cols:
        op.add_column(_CHAINS, sa.Column("status", sa.String(length=32), nullable=True))
    if "commands" not in cols:
        op.add_column(_CHAINS, sa.Column("commands", sa.Text(), nullable=True))


def downgrade() -> None:
    cols = _present(_CHAINS)
    if "commands" in cols:
        op.drop_column(_CHAINS, "commands")
    if "status" in cols:
        op.drop_column(_CHAINS, "status")
