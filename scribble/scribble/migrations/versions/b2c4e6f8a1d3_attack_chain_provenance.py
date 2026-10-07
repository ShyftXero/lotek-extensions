"""LOT-69: attack-chain provenance columns (evidence-first citations)

Adds additive, nullable provenance columns so a minted ``AttackChain`` can cite the deterministic sources
it was produced from (LOT-44 evidence-first doctrine - every claim points at an artifact):

  * ``scribble_attack_chains.source_finding_ids`` (JSON) - the finding ids the chain draws on.
  * ``scribble_attack_chains.rule_ids`` (JSON) - the heterogeneous rule identifiers behind them (Nuclei
    template id, MSF module, CVE/KEV id).
  * ``scribble_attack_chain_steps.finding_id`` (Uuid) - the one finding a single hop is evidenced by.
  * ``scribble_attack_chain_steps.rule_id`` (String) - the one rule identifier behind that hop.

All four are additive + nullable, safe on a populated table: a chain/step created before this column reads
NULL, and every read is NULL-tolerant. No FK on the id columns - the ids are exploiteer/scan output that
need not be rows in THIS scribble database, so a constraint would refuse a legitimate cross-source citation
(the same soft-reference posture ``lotek_finding_id`` already carries). Reversible: ``downgrade`` drops the
four columns.

A fresh database never runs this - it is built from the models (which already declare these columns) and
stamped at head; this only backfills a PRE-EXISTING deployment. Idempotent: guards on the reflected columns.
Continues the SINGLE head (``a1b7c3e05d94``); adds no parallel head (a fork silently breaks the scribble
mount - precedent ext#169).

Revision ID: b2c4e6f8a1d3
Revises: a1b7c3e05d94
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c4e6f8a1d3"
down_revision: str | tuple[str, ...] | None = "a1b7c3e05d94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAINS = "scribble_attack_chains"
_STEPS = "scribble_attack_chain_steps"


def _present(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    chain_cols = _present(_CHAINS)
    if "source_finding_ids" not in chain_cols:
        op.add_column(_CHAINS, sa.Column("source_finding_ids", sa.JSON(), nullable=True))
    if "rule_ids" not in chain_cols:
        op.add_column(_CHAINS, sa.Column("rule_ids", sa.JSON(), nullable=True))

    step_cols = _present(_STEPS)
    if "finding_id" not in step_cols:
        op.add_column(_STEPS, sa.Column("finding_id", sa.Uuid(), nullable=True))
    if "rule_id" not in step_cols:
        op.add_column(_STEPS, sa.Column("rule_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    step_cols = _present(_STEPS)
    if "rule_id" in step_cols:
        op.drop_column(_STEPS, "rule_id")
    if "finding_id" in step_cols:
        op.drop_column(_STEPS, "finding_id")

    chain_cols = _present(_CHAINS)
    if "rule_ids" in chain_cols:
        op.drop_column(_CHAINS, "rule_ids")
    if "source_finding_ids" in chain_cols:
        op.drop_column(_CHAINS, "source_finding_ids")
