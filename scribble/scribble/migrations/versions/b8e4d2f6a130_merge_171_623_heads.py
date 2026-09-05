"""Merge the two migration heads that forked off a7f3b9c1d2e4.

Two branches cut a revision off the same parent (``a7f3b9c1d2e4``, itself the previous merge revision)
and merged independently, leaving the scribble chain with TWO heads:

  * ``a7d2c4e6f810`` — typed ``EngagementFinding`` references + CVE/CWE/OWASP/threat_intel columns
    (PR #171, merged 2026-09-04);
  * ``e5a1c3d7b920`` — lotek#623, ``Engagement.strategic_recommendations`` (PR #180), the tip of the
    chain ``a1b2c3d4e5f6`` (lotek#621 retests, PR #176) → ``d3f5a7c9b1e2`` (lotek#628 attack chains,
    PR #178) → ``e5a1c3d7b920``, all three merged inside ~30 seconds on 2026-09-05.

Neither side rebased onto the other, so both stayed rooted at ``a7f3b9c1d2e4``. Alembic refuses to
``stamp('head')`` or ``upgrade head`` with multiple heads, so scribble failed to mount in lotek
(``run_migrations`` raised, discovery swallowed it → the extension silently did not mount) and every
test that boots the app ERRORed. This is a no-op merge revision: each parent already applied its own
columns and tables; this only rejoins the DAG to a single head so the chain is linear again.

Revision ID: b8e4d2f6a130
Revises: a7d2c4e6f810, e5a1c3d7b920
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "b8e4d2f6a130"
down_revision: str | tuple[str, ...] | None = ("a7d2c4e6f810", "e5a1c3d7b920")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: both parents already applied their column and table additions."""


def downgrade() -> None:
    """No-op: splitting the head back into two is not a schema change."""
