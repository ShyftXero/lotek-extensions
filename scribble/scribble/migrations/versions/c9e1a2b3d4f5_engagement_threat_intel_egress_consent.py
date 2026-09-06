"""Engagement.threat_intel_egress_consent — per-engagement KEV/EPSS egress consent (lotek#642)

Adds ONE boolean column to ``scribble_engagements``:

  * ``threat_intel_egress_consent``  NOT NULL, default FALSE

The per-engagement consent gate for #642 threat_intel (KEV/EPSS) enrichment (INV-EGRESS-02): a
finding's CVEs never leave the box to a third-party feed unless this is explicitly enabled for the
engagement (and it is additionally FORCED OFF for internal engagements, in application code —
``scribble.enrichment.egress_consented``). Off by default is the whole point, so existing rows must
read OFF: ``server_default=false()`` backfills them.

Additive + safe on a populated table (a NOT NULL column WITH a server_default). A fresh database never
runs this — it is built from the models (which declare the column) and stamped at head; this only
backfills a pre-existing deployment. Idempotent (guards on the reflected column, no-op once present).

Plain additive migration chained off the current single head ``b8e4d2f6a130`` (the merge revision that
reunited #171 and #623). NOT a merge revision: depending on a second parent would fork the tree back
into two heads and break ``run_migrations``' ``stamp/upgrade("head")``.

Revision ID: c9e1a2b3d4f5
Revises: b8e4d2f6a130
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e1a2b3d4f5"
down_revision: str | tuple[str, ...] | None = "b8e4d2f6a130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_engagements"
_COLUMN = "threat_intel_egress_consent"


def _present() -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(_TABLE)}


def upgrade() -> None:
    if _COLUMN not in _present():
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if _COLUMN in _present():
        op.drop_column(_TABLE, _COLUMN)
