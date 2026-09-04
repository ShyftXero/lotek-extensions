"""Add scribble_findings.source_facts (verbatim FindingDTO snapshot)

EngagementFinding is a LOSSLESS SUPERSET of the scan FindingDTO (map #616 / #617). ``source_facts`` holds
the full ``host_contract.FindingDTO`` captured VERBATIM as JSON at promote time, so every DTO field is
representable even when it has no typed column, and the source finding's own values survive the
template-match path (where ``from_template`` builds the row from the library template and would otherwise
discard the DTO's title/severity/prose).

Additive + nullable, so it is safe on a populated table: a row promoted before this column existed reads
NULL, and every read site uses ``finding.source_facts or {}``. A fresh database never runs this — it is
built from the models (which already declare the column) and stamped at head; this only backfills a
pre-existing deployment. Idempotent (guards on the reflected columns, no-op once present).

Revision ID: f4c9a1b2e370
Revises: 76a1de5a7c83
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c9a1b2e370"
down_revision: str | None = "76a1de5a7c83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_findings"
_COL = "source_facts"


def _has_column() -> bool:
    insp = sa.inspect(op.get_bind())
    return any(c["name"] == _COL for c in insp.get_columns(_TABLE))


def upgrade() -> None:
    if _has_column():
        return  # already present — nothing to add
    op.add_column(_TABLE, sa.Column(_COL, sa.JSON(), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    op.drop_column(_TABLE, _COL)
