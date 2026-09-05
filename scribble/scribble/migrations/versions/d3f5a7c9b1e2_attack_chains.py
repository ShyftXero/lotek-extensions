"""lotek#628: scribble_attack_chains + scribble_attack_chain_steps

The attack-chain NARRATIVE data model (#628): an ``AttackChain`` is the authored story of how findings
chain into a broader compromise (a ``title`` + ``summary`` over ordered ``AttackChainStep`` rows), with
an OPTIONAL self-contained ``embed_html`` snapshot mirroring ``scribble_engagement_diagram``.

Continues the SINGLE head (``a1b2c3d4e5f6``); adds no parallel head — a fork here would silently break the
scribble mount (precedent ext#169). A fresh ``create_all`` database is built from the models (which already
declare both tables) and stamped at head without ever running this, so it only creates the tables on a
PRE-EXISTING deployment's adoption path. Idempotent: guard on the reflected table rather than assuming
absence, matching the chain's house style.

Revision ID: d3f5a7c9b1e2
Revises: a1b2c3d4e5f6
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f5a7c9b1e2"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAINS = "scribble_attack_chains"
_STEPS = "scribble_attack_chain_steps"


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(_CHAINS):
        op.create_table(
            _CHAINS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("engagement_id", sa.Uuid(), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("diagram_ref", sa.String(length=64), nullable=True),
            sa.Column("embed_html", sa.Text(), nullable=True),
            sa.Column("order_index", sa.Integer(), nullable=False),
            sa.Column("include_in_report", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["engagement_id"], ["scribble_engagements.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f(f"ix_{_CHAINS}_engagement_id"), _CHAINS, ["engagement_id"], unique=False)

    if not insp.has_table(_STEPS):
        op.create_table(
            _STEPS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("chain_id", sa.Uuid(), nullable=False),
            sa.Column("order_index", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["chain_id"], [f"{_CHAINS}.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f(f"ix_{_STEPS}_chain_id"), _STEPS, ["chain_id"], unique=False)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table(_STEPS):
        op.drop_index(op.f(f"ix_{_STEPS}_chain_id"), table_name=_STEPS)
        op.drop_table(_STEPS)
    if insp.has_table(_CHAINS):
        op.drop_index(op.f(f"ix_{_CHAINS}_engagement_id"), table_name=_CHAINS)
        op.drop_table(_CHAINS)
