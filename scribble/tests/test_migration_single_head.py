"""The scribble migration chain must have exactly ONE head.

Two branches once cut a revision off the same parent (``76a1de5a7c83``) and merged independently —
lotek#620 (`risk_override`) and lotek#617 (`source_facts` superset) — leaving the chain with TWO heads.
Alembic refuses to ``stamp('head')`` or ``upgrade head`` on a multi-head chain, so scribble silently
failed to mount in lotek (``run_migrations`` raised and discovery swallowed it). This guard fails the
moment a second head reappears — at the cheap end (a hermetic, no-DB scan of the script directory),
instead of at mount time in the product.
"""
from __future__ import annotations

from pathlib import Path

from alembic.script import ScriptDirectory

import scribble


def test_migration_chain_has_a_single_head():
    script = ScriptDirectory(str(Path(scribble.__file__).resolve().parent / "migrations"))
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"scribble migrations forked into multiple heads: {heads} — add a merge revision "
        "(down_revision = (<head_a>, <head_b>)) to rejoin them."
    )
