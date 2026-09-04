"""Merge the two migration heads that forked off 76a1de5a7c83.

Two branches cut a revision off the same parent (``76a1de5a7c83``) and merged independently, leaving the
scribble chain with TWO heads:

  * ``f0a1c2d3e4b5`` — lotek#620, ``Engagement.risk_override`` + ``risk_override_rationale`` (PR #168);
  * ``f4c9a1b2e370`` — lotek#617, ``EngagementFinding`` source_facts + disposition superset (PR #167).

Alembic refuses to ``stamp('head')`` or ``upgrade head`` with multiple heads, so scribble failed to mount
in lotek (``run_migrations`` raised, discovery swallowed it → the extension silently did not mount). This
is a no-op merge revision: each parent already ADDed its own columns idempotently; this only rejoins the
DAG to a single head so the chain is linear again.

Revision ID: a7f3b9c1d2e4
Revises: f0a1c2d3e4b5, f4c9a1b2e370
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "a7f3b9c1d2e4"
down_revision: str | tuple[str, ...] | None = ("f0a1c2d3e4b5", "f4c9a1b2e370")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: both parents already applied their column additions."""


def downgrade() -> None:
    """No-op: splitting the head back into two is not a schema change."""
