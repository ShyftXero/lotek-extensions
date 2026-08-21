"""Widen scribble_artifacts.idempotency_key from VARCHAR(80) to TEXT

The upload dedup key is derived client-side from the engagement id, finding id and file basename —
lotek's ``attach-evidence.sh`` sends ``ev-<engagement>-<finding>-<basename>-<sha[:32]>``. While those ids
were small sequential integers the key fit inside ``VARCHAR(80)``. #372 migrated scribble's engagement and
finding PKs to 36-char UUIDv7s, which pushes a routine key to ~120 chars, and Postgres then rejects the
INSERT:

    psycopg.errors.StringDataRightTruncation: value too long for type character varying(80)

— a 500 on *every* evidence upload to a UUID-era engagement. It went unseen because SQLite (every unit
test) has no length enforcement and stored the long key unharmed; only real Postgres refuses it. The
basename alone is a ``String(512)``, so no bounded width is safe — widen to TEXT.

Idempotent (guards on the reflected type, no-op once already TEXT) and portable: ``batch_alter_table`` is a
plain ``ALTER COLUMN … TYPE`` on Postgres — which rebuilds the column's index automatically, so the
declared ``index=True`` survives — and a table rebuild on SQLite. A fresh database never runs this: it is
built from the models at TEXT and stamped at head (see ``scribble.db.run_migrations``); this only repairs a
pre-existing deployment.

Revision ID: d7b3f1a4c680
Revises: c2f8a1d3e460
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7b3f1a4c680"
down_revision: str | None = "c2f8a1d3e460"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_artifacts"
_COL = "idempotency_key"


def _current_length() -> int | None:
    """The reflected column's declared length: 80 for the old VARCHAR(80), None once it is TEXT (or the
    column is somehow absent). Guards both directions so re-running is a no-op."""
    insp = sa.inspect(op.get_bind())
    col = next((c for c in insp.get_columns(_TABLE) if c["name"] == _COL), None)
    if col is None:
        return None
    return getattr(col["type"], "length", None)


def upgrade() -> None:
    if _current_length() is None:
        return  # already TEXT (or absent) — nothing to widen
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            _COL, type_=sa.Text(), existing_type=sa.String(length=80), existing_nullable=True,
        )


def downgrade() -> None:
    # Narrow back to VARCHAR(80). Lossy by nature — a row whose key already exceeds 80 chars (any UUID-era
    # upload, the exact case this revision exists for) makes Postgres refuse the shrink. That refusal is
    # correct: you cannot downgrade past data that needs the width. No-op if it is already bounded.
    if _current_length() is not None:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            _COL, type_=sa.String(length=80), existing_type=sa.Text(), existing_nullable=True,
        )
