"""The lotek#656 coverage note lands at most once per (engagement, job): partial-unique (engagement_id, source_job_id)

``coverage.write_coverage_note`` deduped the acknowledged-promotion coverage note in PYTHON, by its
rendered TITLE -- it read ``engagement.findings`` and inserted if no row matched. ext#257 closed the same
read-then-write race for promoted FINDINGS with ``uq_scribble_findings_engagement_source``, but that index
is partial on non-null ``source_finding_id`` and the note carries NULL there (it descends from no scan
finding), so the database never saw the note's duplicate. Two concurrent acknowledged promotions -- a
double-clicked override, the browser and the machine API at once, and in particular a job with ZERO
promotable findings, where nothing else collides to trigger ext#257's retry -- could both read no note and
both insert, doubling the caveat in the deliverable. ext#258.

The fix is a durable identity: a new ``source_job_id`` column holds the host job id the note is FOR (the
title is a rendered string, not an identity, and an operator may legitimately author two findings with the
same title), and this index makes "one note per (engagement, job)" a DATABASE rule.

PARTIAL, on non-null ``source_job_id``: only the coverage note carries a job id. Authored findings,
promoted findings and synthesized parent write-ups all leave it NULL and many share an engagement, so a
total unique index would refuse them.

BACKFILLS existing notes from their title before building the index. A note written before this revision
has ``source_job_id`` NULL, so the new keyed lookup would miss it and re-acknowledging the same job would
write a SECOND note -- the exact bug, reintroduced for legacy rows. The title is the only identity those
rows carry, so the job id is parsed back out of it (``COVERAGE_NOTE_TITLE`` as it stood: "Scan coverage
limitation — job {job_id}") for rows that are unmistakably coverage notes (that title shape, flat, no
source finding).

Self-heals existing duplicates BEFORE creating the index, for the reason ``a1b7c3e05d94`` and
``d1e2f3a4b5c6`` learned the hard way (lotek#914): a ``CREATE UNIQUE INDEX`` that raises rolls the
migration back and scribble fails to MOUNT -- the constraint's whole point is defeated by refusing to
install, and the backfill above is exactly what can surface a pre-existing doubled note as a key clash.
Oldest row per ``(engagement_id, source_job_id)`` keeps the link (smallest id; UUIDv7 sorts in creation
order, matching the note dedup's own "refresh the first one" semantics), and each newer duplicate is
resolved by whether an operator has touched it:

  * **Nothing references it** -- untouched machine output, a note the race created and nobody edited.
    DELETED. Its presence IS the defect, and deleting it restores the state a sequential acknowledgement
    would have produced.
  * **Something references it** -- a child finding, a variable value, an artifact: an operator invested
    work in this copy. KEPT, with ``source_job_id`` nulled so the index can build and
    ``include_in_report`` cleared so the deliverable stops showing the duplicate.

Referencing tables are REFLECTED from the database's own foreign keys, not hardcoded (mirrors
``a1b7c3e05d94``), so a table added after this revision still counts as "an operator touched this row".

A fresh ``create_all`` database is built from the models -- which now declare this column and index -- and
stamped at head, so this revision only ever runs on a PRE-EXISTING deployment. Idempotent: guards on the
reflected column and index.

Revision ID: f3b8c1e07a92
Revises: e5f1a2b3c4d7
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8c1e07a92"
down_revision: str | tuple[str, ...] | None = "e5f1a2b3c4d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_findings"
_COL = "source_job_id"
_UQ = "uq_scribble_findings_engagement_job"
# The title as COVERAGE_NOTE_TITLE rendered it. Pinned here, not imported: a migration is a snapshot of
# the schema as it was, and parsing legacy rows must not shift if the live template is reworded.
_TITLE_PREFIX = "Scan coverage limitation — job "


def _referencing_columns(insp) -> list[tuple[str, str]]:
    """Every ``(table, column)`` that foreign-keys to ``scribble_findings.id`` (incl. ``parent_id``)."""
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

    if _COL not in {c["name"] for c in insp.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COL, sa.String(64), nullable=True))

    # Backfill: parse the job id back out of the title for rows that are unmistakably coverage notes --
    # that title shape, flat (no group), no source finding, and not already keyed.
    rows = bind.execute(
        sa.text(
            f"SELECT id, title FROM {_TABLE} "  # noqa: S608
            f"WHERE {_COL} IS NULL AND group_id IS NULL AND source_finding_id IS NULL "
            "AND title LIKE :like"
        ),
        {"like": _TITLE_PREFIX + "%"},  # the prefix carries no LIKE wildcards; startswith re-checks below
    ).fetchall()
    for rid, title in rows:
        if not title or not title.startswith(_TITLE_PREFIX):
            continue
        job_id = title[len(_TITLE_PREFIX):]
        if not job_id:
            continue
        bind.execute(
            sa.text(f"UPDATE {_TABLE} SET {_COL} = :jid WHERE id = :id"),  # noqa: S608
            {"jid": job_id, "id": rid},
        )

    # Self-heal duplicates the backfill may have surfaced, before the unique index would reject them.
    pairs = _referencing_columns(insp)
    keyed = bind.execute(
        sa.text(
            f"SELECT id, engagement_id, {_COL} FROM {_TABLE} WHERE {_COL} IS NOT NULL"  # noqa: S608
        )
    ).fetchall()
    by_key: dict[tuple[str, str], list] = {}
    for rid, eid, jid in keyed:
        by_key.setdefault((str(eid), str(jid)), []).append(rid)

    for rids in by_key.values():
        if len(rids) < 2:
            continue
        for rid in sorted(rids, key=str)[1:]:  # keep the oldest; resolve the rest
            if _is_referenced(bind, pairs, rid):
                bind.execute(
                    sa.text(
                        f"UPDATE {_TABLE} SET {_COL} = NULL, "  # noqa: S608
                        "include_in_report = :off WHERE id = :id"
                    ),
                    {"id": rid, "off": False},
                )
            else:
                bind.execute(sa.text(f"DELETE FROM {_TABLE} WHERE id = :id"), {"id": rid})  # noqa: S608

    if _UQ not in {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}:
        op.create_index(
            _UQ, _TABLE, ["engagement_id", _COL], unique=True,
            postgresql_where=sa.text(f"{_COL} IS NOT NULL"),
            sqlite_where=sa.text(f"{_COL} IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # The dedup above is NOT reversed: a deleted duplicate is gone and a nulled link cannot be told from a
    # genuinely authored finding. Drop the index and the column; the duplicates do not come back.
    if insp.has_table(_TABLE):
        if _UQ in {i["name"] for i in insp.get_indexes(_TABLE)}:
            op.drop_index(_UQ, table_name=_TABLE)
        if _COL in {c["name"] for c in insp.get_columns(_TABLE)}:
            op.drop_column(_TABLE, _COL)
