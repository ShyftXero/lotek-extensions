"""The ext#258 migration (`f3b8c1e07a92`) must BACKFILL `source_job_id` from the title and self-heal
duplicate coverage notes BEFORE it builds the partial-unique index.

Two things are novel here versus the sibling ext#257 migration, and both are backend-agnostic (so this
runs on SQLite, unlike the Postgres-gated `test_promote_dedup_migration`):

  * A note written before this revision has `source_job_id` NULL. The new keyed lookup in
    `write_coverage_note` would then miss it and re-acknowledging the same job would write a SECOND note --
    the exact bug, reintroduced for legacy rows. The title is the only identity those rows carry, so the
    job id is parsed back out of it.
  * The backfill can surface a pre-existing doubled note as a duplicate key. The migration resolves those
    first (keep the oldest, delete the untouched rest) or `CREATE UNIQUE INDEX` aborts and scribble fails
    to mount (lotek#914).

The index is dropped (not the column) to stand in for "before this revision": the create_all schema this
test starts from already carries both, and re-adding the column is the one trivial, proven-pattern step
not worth reconstructing a legacy schema to exercise.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

import scribble.models as fm

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "scribble/migrations/versions/f3b8c1e07a92_coverage_note_engagement_job_unique.py"
)
_TITLE = "Scan coverage limitation — job {job_id}"


def _load_migration():
    spec = importlib.util.spec_from_file_location("ext258_migration", _MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _note(engagement_id, job_id, **kw):
    return fm.BoardFinding(
        engagement_id=engagement_id,
        title=_TITLE.format(job_id=job_id),
        group_id=None,
        source_finding_id=None,
        source_job_id=None,  # legacy: written before this revision existed
        **kw,
    )


def test_the_migration_backfills_from_title_then_dedups_then_builds_the_index(app, session_factory):
    with session_factory() as db:
        board = fm.ReportBoard(name="ext258 migration", scope_type="internal")
        db.add(board)
        db.commit()
        bid = board.id
        # Two legacy notes for job-1 (the race already doubled it), one for job-2, and a decoy that WEARS
        # the coverage-note title but is a promoted finding (source_finding_id set) and must be left alone.
        db.add(_note(bid, "job-1"))
        db.add(_note(bid, "job-1"))
        db.add(_note(bid, "job-2"))
        decoy = _note(bid, "job-9")
        decoy.source_finding_id = uuid.uuid7()
        db.add(decoy)
        db.commit()
        engine = db.get_bind()

    # Stand in for "before this revision": drop the index the model's create_all already built.
    with engine.begin() as conn:
        conn.execute(text(f"DROP INDEX {fm.COVERAGE_NOTE_DEDUP_INDEX}"))
    assert fm.COVERAGE_NOTE_DEDUP_INDEX not in {
        i["name"] for i in inspect(engine).get_indexes("scribble_findings")
    }

    mod = _load_migration()
    with engine.begin() as conn:
        mod.op = Operations(MigrationContext.configure(conn))
        mod.upgrade()

    with engine.connect() as c:
        job1 = c.execute(
            text("SELECT source_job_id FROM scribble_findings WHERE source_job_id = 'job-1'")
        ).scalars().all()
        job2 = c.execute(
            text("SELECT source_job_id FROM scribble_findings WHERE source_job_id = 'job-2'")
        ).scalars().all()
        decoy_sjid = c.execute(
            text("SELECT source_job_id FROM scribble_findings WHERE source_finding_id IS NOT NULL")
        ).scalar_one()

    assert job1 == ["job-1"], f"the doubled legacy note must collapse to one, backfilled; got {job1}"
    assert job2 == ["job-2"], f"the single legacy note must be backfilled; got {job2}"
    assert decoy_sjid is None, "a promoted finding wearing the note's title is NOT a coverage note"
    assert fm.COVERAGE_NOTE_DEDUP_INDEX in {
        i["name"] for i in inspect(engine).get_indexes("scribble_findings")
    }, "the partial-unique index must exist after the migration"
