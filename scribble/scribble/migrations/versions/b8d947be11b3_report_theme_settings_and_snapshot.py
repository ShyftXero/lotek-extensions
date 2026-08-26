"""Report Theme settings singleton + per-engagement Theme choice/Snapshot (#100, #105, #106)

Schema half of the Layout/Theme split's install-wide default and per-engagement selection:

1. **scribble_settings** -- a new singleton table (``slot`` unique, always ``"default"``, same pattern
   as CREAM's ``cream_brand``) carrying ``default_report_theme``: the Theme name an install falls back
   to when an engagement does not choose its own. Kept minimal on purpose -- see
   ``scribble.models.ScribbleSettings``'s docstring for why this is not folded into
   ``scribble_variables``.
2. **scribble_engagements.report_theme** -- the chosen Theme name, NULLABLE (NULL = inherit the
   install default, which itself may fall back to ``scribble.reporting.themes.DEFAULT_THEME``).
3. **scribble_engagements.report_theme_snapshot** -- JSON, the RESOLVED tokens/marks frozen at
   delivery, so a Report already in a client's hands keeps rendering as delivered even after the Theme
   it came from is edited or uninstalled. See ``scribble.models.Engagement``'s docstring for the full
   reasoning (mirrors why CREAM snapshots ``Brand`` onto an issued ``Document``).

Both new ``scribble_engagements`` columns are NULLABLE with no server default, so this is a plain
additive change for every existing row -- no backfill, no data migration.

Revision ID: b8d947be11b3
Revises: d7b3f1a4c680
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d947be11b3"
down_revision: str | None = "d7b3f1a4c680"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTINGS_TABLE = "scribble_settings"
_ENGAGEMENTS_TABLE = "scribble_engagements"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    if not insp.has_table(_SETTINGS_TABLE):
        op.create_table(
            _SETTINGS_TABLE,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("slot", sa.String(length=16), nullable=False),
            sa.Column("default_report_theme", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("slot"),
        )

    engagement_cols = {c["name"] for c in insp.get_columns(_ENGAGEMENTS_TABLE)}
    if "report_theme" not in engagement_cols:
        op.add_column(
            _ENGAGEMENTS_TABLE, sa.Column("report_theme", sa.String(length=64), nullable=True)
        )
    if "report_theme_snapshot" not in engagement_cols:
        op.add_column(
            _ENGAGEMENTS_TABLE, sa.Column("report_theme_snapshot", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    engagement_cols = {c["name"] for c in insp.get_columns(_ENGAGEMENTS_TABLE)}
    if "report_theme_snapshot" in engagement_cols:
        op.drop_column(_ENGAGEMENTS_TABLE, "report_theme_snapshot")
    if "report_theme" in engagement_cols:
        op.drop_column(_ENGAGEMENTS_TABLE, "report_theme")
    if insp.has_table(_SETTINGS_TABLE):
        op.drop_table(_SETTINGS_TABLE)
