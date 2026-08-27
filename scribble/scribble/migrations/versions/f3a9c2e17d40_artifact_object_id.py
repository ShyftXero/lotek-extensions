"""artifact.object_id — evidence bytes in the core object store

Adds the pointer that lets an artifact's bytes live in SeaweedFS (via the host contract's
``HostObjects``) instead of on the dashboard's local filesystem.

Deliberately ADDITIVE and nullable. ``storage_path`` stays and stays populated for pre-cutover rows:
this is a cutover with a read fallback, not a flag day, and there is no backfill here. A row has
either an ``object_id`` (new) or a usable ``storage_path`` (old); the read path tries the store first
and falls back to disk, so an upgrade does not strip evidence off existing reports.

Revision ID: f3a9c2e17d40
Revises: d7b3f1a4c680
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from scribble.models import ScribbleUuid

revision: str = "f3a9c2e17d40"
down_revision: str | None = "d7b3f1a4c680"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("scribble_artifacts") as batch:
        batch.add_column(sa.Column("object_id", ScribbleUuid(), nullable=True))
    op.create_index("ix_scribble_artifacts_object_id", "scribble_artifacts", ["object_id"])


def downgrade() -> None:
    op.drop_index("ix_scribble_artifacts_object_id", table_name="scribble_artifacts")
    with op.batch_alter_table("scribble_artifacts") as batch:
        batch.drop_column("object_id")
