"""One scan finding pours into a report board at most once: partial-unique (engagement_id, source_finding_id)

``scribble.promote.promote_job`` deduped a re-promotion in PYTHON -- it read ``engagement.findings``
into sets and then inserted -- so two CONCURRENT promotions of the same job into the same board (a
double-clicked "Add to report", a client retry, the browser and the machine API at once) could both
build their sets before either committed, and both insert. The deliverable then carried every promoted
finding twice. Nothing in the schema stopped it: ``source_finding_id`` is indexed ``unique=False``
(baseline ``e17599b0880a``) and ``BoardFinding`` declared no ``__table_args__`` at all. ext#257.

PARTIAL, on non-null ``source_finding_id``: author-written findings, synthesized parent write-ups and
the lotek#656 coverage note all legitimately carry NULL there and many share an engagement.

Self-heals existing duplicates BEFORE creating the index, for the reason ``d1e2f3a4b5c6`` learned the
hard way (lotek#914): a ``CREATE UNIQUE INDEX`` that raises rolls the migration back and scribble fails
to MOUNT -- the constraint's whole point is defeated by refusing to install. Oldest row per
``(engagement_id, source_finding_id)`` keeps the link (smallest id; UUIDv7 sorts in creation order,
matching the dedup's own "already promoted" semantics), and each newer duplicate is resolved by whether
an operator has touched it:

  * **Nothing references it** -- untouched machine output, a row the race created and nobody edited.
    DELETED. Its presence IS the defect, and deleting it restores exactly the state a sequential promote
    would have produced.
  * **Something references it** -- a variable value, a prose override, an artifact, a child finding: an
    operator invested work in this copy. KEPT, with ``source_finding_id`` nulled so the index can build
    and ``include_in_report`` cleared so the deliverable stops showing the duplicate. Nothing an operator
    wrote is destroyed; the row survives as an authored finding they can re-include or delete.

The referencing tables are REFLECTED from the database's own foreign keys, not hardcoded: seven tables
point at ``scribble_findings.id`` today and a hardcoded list would silently miss the eighth.

A fresh ``create_all`` database is built from the models -- which now declare this index -- and stamped
at head, so this revision only ever runs on a PRE-EXISTING deployment. Idempotent: guards on the
reflected table and index.

Revision ID: a1b7c3e05d94
Revises: f7a1d4c8e520
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b7c3e05d94"
down_revision: str | tuple[str, ...] | None = "f7a1d4c8e520"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_findings"
_UQ = "uq_scribble_findings_engagement_source"


def _referencing_columns(insp) -> list[tuple[str, str]]:
    """Every ``(table, column)`` in the database that foreign-keys to ``scribble_findings.id``.

    Reflected rather than hardcoded so a table added after this revision is written still counts as
    "an operator touched this row". Includes ``scribble_findings.parent_id`` -- a duplicate that is
    somebody's PARENT must not be deleted out from under its children.
    """
    pairs: list[tuple[str, str]] = []
    for table in insp.get_table_names():
        for fk in insp.get_foreign_keys(table):
            if fk.get("referred_table") != _TABLE:
                continue
            for local, remote in zip(fk.get("constrained_columns") or (),
                                     fk.get("referred_columns") or (), strict=False):
                if remote == "id":
                    pairs.append((table, local))
    return pairs


def _is_referenced(bind, pairs: list[tuple[str, str]], row_id) -> bool:
    for table, column in pairs:
        hit = bind.execute(
            sa.text(f'SELECT 1 FROM "{table}" WHERE "{column}" = :id LIMIT 1'),  # noqa: S608
            {"id": row_id},
        ).first()
        if hit is not None:
            return True
    return False


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table(_TABLE):
        return

    pairs = _referencing_columns(insp)
    rows = bind.execute(
        sa.text(
            f"SELECT id, engagement_id, source_finding_id FROM {_TABLE} "  # noqa: S608
            "WHERE source_finding_id IS NOT NULL"
        )
    ).fetchall()

    by_key: dict[tuple[str, str], list] = {}
    for rid, eid, sfid in rows:
        by_key.setdefault((str(eid), str(sfid)), []).append(rid)

    for rids in by_key.values():
        if len(rids) < 2:
            continue
        for rid in sorted(rids, key=str)[1:]:  # keep the oldest; resolve the rest
            if _is_referenced(bind, pairs, rid):
                bind.execute(
                    sa.text(
                        f"UPDATE {_TABLE} SET source_finding_id = NULL, "  # noqa: S608
                        "include_in_report = :off WHERE id = :id"
                    ),
                    {"id": rid, "off": False},
                )
            else:
                bind.execute(sa.text(f"DELETE FROM {_TABLE} WHERE id = :id"), {"id": rid})  # noqa: S608

    if _UQ not in {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}:
        op.create_index(
            _UQ, _TABLE, ["engagement_id", "source_finding_id"], unique=True,
            postgresql_where=sa.text("source_finding_id IS NOT NULL"),
            sqlite_where=sa.text("source_finding_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # The dedup above is NOT reversed: a deleted duplicate is gone and a nulled link cannot be told from
    # a genuinely authored finding. Dropping the index restores the schema, never the duplicates.
    if insp.has_table(_TABLE) and _UQ in {i["name"] for i in insp.get_indexes(_TABLE)}:
        op.drop_index(_UQ, table_name=_TABLE)
