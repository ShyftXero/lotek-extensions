"""vuln-map tag/cwe match columns (tag/cwe -> template resolution)

Adds two additive, nullable match columns to ``scribble_vuln_map`` so a promoted finding can resolve to a
library template by its tool CLASSIFICATION, not only by ``dedupe_prefix``/``title_pattern``/``source``:

  * ``scribble_vuln_map.tag`` (String) - matches ONE of the finding's ``facts['tags']`` (e.g. nuclei's
    own ``sqli``/``lfi``/``xss`` tags). The tag IS the tool's classification, so ~15 tag rows cover
    thousands of nuclei templates and a new template inherits its tag's mapping for free.
  * ``scribble_vuln_map.cwe_id`` (String) - matches the finding's ``facts['cwe']`` (e.g. "CWE-89").

Both are additive + nullable, safe on a populated table: an existing map row reads NULL for both (so it
keeps resolving exactly as before — these only add NEW, lower-precedence ways to match). Indexed to mirror
``source``/``dedupe_prefix``. Reversible: ``downgrade`` drops the two columns.

A fresh database never runs this - it is built from the models (which already declare these columns) and
stamped at head; this only backfills a PRE-EXISTING deployment. Idempotent: guards on the reflected
columns. Continues the SINGLE head (``b2c4e6f8a1d3``); adds no parallel head (a fork silently breaks the
scribble mount - precedent ext#169).

Revision ID: c8e1a4f7b230
Revises: b2c4e6f8a1d3
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e1a4f7b230"
down_revision: str | tuple[str, ...] | None = "b2c4e6f8a1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAP = "scribble_vuln_map"


def _present() -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(_MAP)}


def upgrade() -> None:
    cols = _present()
    if "tag" not in cols:
        op.add_column(_MAP, sa.Column("tag", sa.String(length=64), nullable=True))
        op.create_index(op.f("ix_scribble_vuln_map_tag"), _MAP, ["tag"])
    if "cwe_id" not in cols:
        op.add_column(_MAP, sa.Column("cwe_id", sa.String(length=32), nullable=True))
        op.create_index(op.f("ix_scribble_vuln_map_cwe_id"), _MAP, ["cwe_id"])


def downgrade() -> None:
    cols = _present()
    if "cwe_id" in cols:
        op.drop_index(op.f("ix_scribble_vuln_map_cwe_id"), _MAP)
        op.drop_column(_MAP, "cwe_id")
    if "tag" in cols:
        op.drop_index(op.f("ix_scribble_vuln_map_tag"), _MAP)
        op.drop_column(_MAP, "tag")
