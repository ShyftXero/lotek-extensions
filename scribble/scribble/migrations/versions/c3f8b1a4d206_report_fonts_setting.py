"""ScribbleSettings.report_body_font + report_code_font: per-install report font selection

Two additive, nullable columns holding the operator's chosen body + code faces for the report render
(reporting.fonts). NULL = the template's baked default. Safe on a populated table (a row created before
these columns reads NULL); a fresh database is built from the models and stamped at head, so this only
backfills a pre-existing deployment. Idempotent (guards on the reflected columns).

Revision ID: c3f8b1a4d206
Revises: b2e4f6a8c1d3
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f8b1a4d206"
down_revision: str | tuple[str, ...] | None = "b2e4f6a8c1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_theme_settings"
_COLUMNS = ("report_body_font", "report_code_font")


def _present() -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(_TABLE)}


def upgrade() -> None:
    have = _present()
    for col in _COLUMNS:
        if col not in have:
            op.add_column(_TABLE, sa.Column(col, sa.String(length=64), nullable=True))


def downgrade() -> None:
    have = _present()
    for col in reversed(_COLUMNS):
        if col in have:
            op.drop_column(_TABLE, col)
