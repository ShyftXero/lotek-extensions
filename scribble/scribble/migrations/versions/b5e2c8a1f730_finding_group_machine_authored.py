"""ext#402: ``scribble_finding_groups.machine_authored`` — section provenance

One additive, defaulted column so a destructive un-adopt can tell the section the ADOPT path created from
one a person authored. Issue #402's contract: "An empty automatically-created section should not appear
as a stale job block. Do not delete a manually authored section silently." Until now no column separated
the two -- ``name`` and ``assessment_type_id`` are set by both paths (a human's "Report section type"
select assigns an assessment type too) -- so provenance has to be recorded.

``machine_authored`` is BOOLEAN NOT NULL DEFAULT false: every pre-existing section reads false, i.e.
hand-authored, which is the CONSERVATIVE answer for a delete decision (an unmarked section is never
removed by an un-adopt). Only ``findings_service._find_or_create_section`` sets it true, and only on the
create branch, so a section a person made first is reused rather than adopted by the machine.

Safe on a populated table (a NOT NULL column with a server default), reversible, and idempotent (guards
on the reflected columns). Continues the SINGLE head (``c7d9f1b3e5a2``); adds no parallel head (a fork
silently breaks the scribble mount - precedent ext#169).

Revision ID: b5e2c8a1f730
Revises: c7d9f1b3e5a2
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e2c8a1f730"
down_revision: str | tuple[str, ...] | None = "c7d9f1b3e5a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUPS = "scribble_finding_groups"
_COLUMN = "machine_authored"


def _present(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(_GROUPS):
        return  # nothing to add the column to; `create_all` + a stamp built it correctly
    existing = {c["name"]: c for c in insp.get_columns(_GROUPS)}.get(_COLUMN)
    if existing is None:
        op.add_column(
            _GROUPS,
            sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        return
    # The column is ALREADY there -- and it may not be the trusted one. On the ADOPTION path
    # (`db.run_migrations` -> `_additive_column_sync`) every DEFAULTED model column is added as a bare
    # `ALTER TABLE ... ADD COLUMN ... BOOLEAN`: nullable, no server default, and every existing row NULL.
    # This revision then runs and, without the branch below, used to no-op on presence alone -- leaving a
    # DB whose column the model declares NOT NULL but which is NULLABLE and full of NULLs, forever.
    # Harmless today (a NULL is falsy, i.e. the conservative "hand-authored"), but a future
    # `WHERE machine_authored IS FALSE` reader would silently drop every one of those rows -- the same
    # drift class as the SoftHostId INTEGER column `_widen_soft_host_id_columns` exists for.
    # So: presence is not enough. Tighten it, backfilling first (SET NOT NULL over NULLs would fail).
    if not existing["nullable"]:
        return  # already the declared shape (a normal Alembic-era upgrade) -- idempotent
    op.execute(f'UPDATE "{_GROUPS}" SET "{_COLUMN}" = false WHERE "{_COLUMN}" IS NULL')
    with op.batch_alter_table(_GROUPS) as batch:  # a plain ALTER COLUMN on PG; a rebuild on SQLite
        batch.alter_column(
            _COLUMN, existing_type=sa.Boolean(), nullable=False, server_default=sa.false(),
        )


def downgrade() -> None:
    if _COLUMN not in _present(_GROUPS):
        return
    with op.batch_alter_table(_GROUPS) as batch:  # SQLite cannot DROP COLUMN before 3.35
        batch.drop_column(_COLUMN)
