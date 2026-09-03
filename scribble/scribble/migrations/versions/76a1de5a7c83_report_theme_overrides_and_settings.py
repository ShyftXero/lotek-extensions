"""Override report Themes + the per-install default Theme setting (ext#113, #105)

Schema half of the operator-supplied ("override" — see scribble/CONTEXT.md's Provenance entry) Theme
CRUD surface added in ``scribble/themes_api.py``:

1. **scribble_theme_overrides** — one row per operator-uploaded Theme. ``source_toml`` is the
   operator's ORIGINAL TOML text, stored verbatim (never a parsed blob) so it can be re-edited and
   re-validated against ``reporting.theme_files._parse_theme_toml`` — the one true validator every
   other Theme (bundled, and installed once resolved) is checked against too. ``name`` is UNIQUE and is
   kept equal to the stored TOML's own ``[identity].name`` by the API layer (see
   ``scribble.models.ScribbleThemeOverride``'s docstring); it is never a bundled Theme name, refused by
   the API before a row is ever written.
2. **scribble_theme_settings** — a singleton table (``slot`` unique, always ``"default"``, the same
   pattern as CREAM's ``cream_brand``) carrying ``default_report_theme``: the Theme name an install
   falls back to when a Report does not choose its own. Investigated first per this ticket's brief
   (does a generic host `[[settings]]` seam already exist here the way Vector's does? — no:
   ``scribble/lotek-extension.toml`` declares no ``[[settings]]`` table and ``scribble/deps.py`` has no
   ``host_setting`` accessor), so this is the minimal Scribble-owned fallback, not a duplicate of a seam
   that could have been reused.

This recovers the SHAPE ``75159ed`` cut from an earlier iteration of this branch (``scribble_settings``
+ ``b8d947be11b3``) but deliberately narrower: that cut also carried ``Engagement.report_theme`` /
``Engagement.report_theme_snapshot`` (per-ENGAGEMENT Theme choice + delivery Snapshot), which remain out
of scope here — nothing in this repo yet reads or writes a per-engagement Theme column, and adding one
without a caller is exactly the "840 lines with no caller" mistake that commit was cutting. Table
renamed ``scribble_theme_settings`` (was ``scribble_settings``) since this ticket's investigation
concluded a general-purpose Scribble settings table is not (yet) needed — only this one Theme knob is —
so a narrower, more honestly-named table beats a generically-named one carrying a single column.

Both tables are created fresh; a fresh database never runs this migration at all (see
``scribble.db.run_migrations``'s "fresh database" branch — it builds straight from the models and
stamps head), so idempotent ``has_table`` guards are what let an EXISTING deployment adopt this
revision safely, exactly like every other migration in this chain.

Revision ID: 76a1de5a7c83
Revises: d7b3f1a4c680
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "76a1de5a7c83"
down_revision: str | None = "d7b3f1a4c680"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OVERRIDES_TABLE = "scribble_theme_overrides"
_SETTINGS_TABLE = "scribble_theme_settings"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())

    if not insp.has_table(_OVERRIDES_TABLE):
        op.create_table(
            _OVERRIDES_TABLE,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("source_toml", sa.Text(), nullable=False),
            sa.Column("created_by", sa.String(length=128), nullable=True),
            sa.Column("updated_by", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

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


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table(_SETTINGS_TABLE):
        op.drop_table(_SETTINGS_TABLE)
    if insp.has_table(_OVERRIDES_TABLE):
        op.drop_table(_OVERRIDES_TABLE)
