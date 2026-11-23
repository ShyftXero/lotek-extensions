"""Widen scribble_findings.target_url (1024) and .title (512) to TEXT

A promoted finding's ``target_url`` is evidence. A scanner can legitimately report a URL longer than
1024 characters: dalfox writing a crawl result into ``matched_at`` produced a comma-joined pair that
overflowed the column, and Postgres refused the promotion UPDATE:

    psycopg.errors.StringDataRightTruncation: value too long for type character varying(1024)

which surfaced to the operator as a 500 on "Adopt job" (lotek-extensions#402). SQLite, which every unit
test uses, does not enforce the length, so the suite stayed green.

Widening (rather than truncating) keeps the evidence intact. Idempotent: it guards on the reflected type,
so a re-run is a no-op, and a fresh database is built from the models at TEXT and stamped at head.

Revision ID: e9c4a2b7f310
Revises: c7d9f1b3e5a2
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9c4a2b7f310"
down_revision: str | None = "c7d9f1b3e5a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_findings"
#: (column, old width, nullable) for every column that carries host-supplied text and must not be
#: bounded. The title comes from the host finding (1024 there) and used to be VARCHAR(512) here.
_COLUMNS: tuple[tuple[str, int, bool], ...] = (
    ("target_url", 1024, True),
    ("title", 512, False),
)


def _current_length(col_name: str) -> int | None:
    """The reflected column's declared length, or None once it is TEXT (or absent)."""
    insp = sa.inspect(op.get_bind())
    col = next((c for c in insp.get_columns(_TABLE) if c["name"] == col_name), None)
    if col is None:
        return None
    return getattr(col["type"], "length", None)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        for name, width, nullable in _COLUMNS:
            if _current_length(name) is None:
                continue  # already TEXT (or absent) - nothing to widen
            batch.alter_column(
                name, type_=sa.Text(), existing_type=sa.String(length=width), existing_nullable=nullable,
            )


def downgrade() -> None:
    # Narrow back. Lossy by nature: a row holding a longer value makes Postgres refuse the shrink, which is
    # the correct answer. No-op for a column that is already bounded.
    with op.batch_alter_table(_TABLE) as batch:
        for name, width, nullable in _COLUMNS:
            if _current_length(name) is not None:
                continue
            batch.alter_column(
                name, type_=sa.String(length=width), existing_type=sa.Text(), existing_nullable=nullable,
            )
