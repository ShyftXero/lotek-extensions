"""lotek#621: scribble_retests + scribble_artifacts.retest_id

The retest DATA MODEL that feeds the retest UI/API (#622): a ``scribble_retests`` row records one
verify-the-fix round against a finding (``outcome`` a :class:`~scribble.enums.RetestOutcome`), and
``scribble_artifacts.retest_id`` links retest evidence back to the round it belongs to.

Continues the SINGLE head (``a7f3b9c1d2e4``); adds no parallel head. A fresh ``create_all`` database is
built from the models (which already declare both) and stamped at head without ever running this — so
this only backfills a PRE-EXISTING deployment on the adoption path. Idempotent, matching the chain's
house style: guard on the reflected table/column rather than assuming absence, since the adoption path's
``_additive_column_sync`` may have ALTER-ADDed ``retest_id`` ahead of this.

``retest_id`` is a plain ``Uuid`` column with NO foreign-key constraint — it mirrors the model's SOFT
reference (``ScribbleUuid``, like ``EngagementChecklist.template_id``); see the column's docstring in
``models.py`` for why a hard FK would be a delete-ordering hazard rather than an integrity win.

Revision ID: a1b2c3d4e5f6
Revises: a7f3b9c1d2e4
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "a7f3b9c1d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETESTS = "scribble_retests"
_ARTIFACTS = "scribble_artifacts"
# A NEW enum type (unlike ``severity``, which the baseline already CREATEd): let create_type default to
# True so ``upgrade`` builds it on Postgres. StrEnum name == value, so the stored strings match the model.
_OUTCOME = sa.Enum(
    "remediated", "partially_remediated", "not_remediated", "accepted_risk", "not_tested",
    name="retestoutcome",
)


def _artifacts_has_retest_id() -> bool:
    insp = sa.inspect(op.get_bind())
    return any(c["name"] == "retest_id" for c in insp.get_columns(_ARTIFACTS))


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(_RETESTS):
        op.create_table(
            _RETESTS,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("finding_id", sa.Uuid(), nullable=False),
            sa.Column("outcome", _OUTCOME, nullable=False),
            sa.Column("tested_on", sa.Date(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("tested_by", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["finding_id"], ["scribble_findings.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f(f"ix_{_RETESTS}_finding_id"), _RETESTS, ["finding_id"], unique=False)

    if not _artifacts_has_retest_id():
        # Soft ref: a bare Uuid column, no ForeignKeyConstraint (see the module + model docstrings).
        op.add_column(_ARTIFACTS, sa.Column("retest_id", sa.Uuid(), nullable=True))
        op.create_index(op.f(f"ix_{_ARTIFACTS}_retest_id"), _ARTIFACTS, ["retest_id"], unique=False)


def downgrade() -> None:
    if _artifacts_has_retest_id():
        op.drop_index(op.f(f"ix_{_ARTIFACTS}_retest_id"), table_name=_ARTIFACTS)
        op.drop_column(_ARTIFACTS, "retest_id")
    if sa.inspect(op.get_bind()).has_table(_RETESTS):
        op.drop_index(op.f(f"ix_{_RETESTS}_finding_id"), table_name=_RETESTS)
        op.drop_table(_RETESTS)
        _OUTCOME.drop(op.get_bind(), checkfirst=True)  # drop the enum type too (no-op on SQLite)
