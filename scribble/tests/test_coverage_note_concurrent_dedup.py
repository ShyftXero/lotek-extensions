"""The lotek#656 coverage note lands AT MOST ONCE per (engagement, job), even under concurrency (ext#258).

`coverage.write_coverage_note` deduped the note by its rendered TITLE: it read `engagement.findings` and
inserted if nothing matched. ext#257 closed the same read-then-write race for promoted findings, but its
index is partial on non-null `source_finding_id` and the note carries NULL there (it descends from no scan
finding), so the database never saw the note's duplicate. Two concurrent acknowledged promotions -- and in
particular a job with ZERO promotable findings, where nothing else collides to trigger ext#257's retry --
could both read no note and both insert, doubling the caveat in the deliverable.

The same three things ext#257 needed, keyed now on a real `source_job_id` column:

1. The DATABASE refuses the duplicate -- a Python guard cannot close a read-then-write window. The index
   must be on the MODEL (not only the alembic revision), because `run_migrations` builds a fresh schema
   from `create_all` and stamps head without replaying the chain.
2. Authored rows stay legal. The index is partial on non-null `source_job_id` precisely so authored
   findings, promoted findings and parent write-ups -- all NULL there, many per engagement -- are not
   collateral damage.
3. The LOSER of the race answers like a sequential re-acknowledge: it refreshes the winner's note in
   place, not a 500 and not a second note.

`test_the_loser_of_a_real_race_refreshes_instead_of_duplicating` drives a genuine interleaving through two
live sessions against the same file-backed SQLite database, both reading before either commits. It is the
one that fails loudly without the fix.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

import scribble.coverage as coverage
import scribble.models as fm

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "scribble/migrations/versions/f3b8c1e07a92_coverage_note_engagement_job_unique.py"
)

# write_coverage_note only reads `assessed` / `unassessed_modules` off the job, for the note's prose.
_JOB = SimpleNamespace(assessed=False, unassessed_modules=("nuclei", "enum4linux"))


def _board(db) -> fm.ReportBoard:
    board = fm.ReportBoard(name="ext258 board", scope_type="internal")
    db.add(board)
    db.commit()
    return board


def test_a_fresh_database_carries_the_partial_unique_index(app, session_factory):
    """The index must be on the MODEL, not only in the alembic revision -- a fresh database is built from
    `create_all` and stamped head without replaying the chain, so a migration-only index would be absent
    from every new deployment and every unit test."""
    with session_factory() as db:
        names = {i["name"] for i in inspect(db.get_bind()).get_indexes("scribble_findings")}
    assert fm.COVERAGE_NOTE_DEDUP_INDEX in names, (
        f"{fm.COVERAGE_NOTE_DEDUP_INDEX} must exist on a database the product actually builds; "
        f"got {sorted(names)}"
    )


def test_the_migration_and_the_model_name_the_same_index():
    """The alembic revision repeats the literal on purpose (a migration is pinned to schema-as-it-was),
    so nothing but this assertion stops the two from drifting -- and a drifted pair means an existing
    deployment gets an index `is_coverage_note_dedup_conflict` cannot recognise."""
    assert fm.COVERAGE_NOTE_DEDUP_INDEX in _MIGRATION.read_text(), (
        f"{_MIGRATION.name} must build the index the model declares ({fm.COVERAGE_NOTE_DEDUP_INDEX})"
    )


def test_the_index_refuses_a_duplicate_source_job_in_one_engagement(app, session_factory):
    """The constraint bites at the DB level -- the claim the regression test rests on."""
    with session_factory() as db:
        board = _board(db)
        db.add(fm.BoardFinding(engagement_id=board.id, title="note a", source_job_id="job-1"))
        db.commit()
        db.add(fm.BoardFinding(engagement_id=board.id, title="note a (dup)", source_job_id="job-1"))
        with pytest.raises(IntegrityError) as caught:
            db.commit()
    assert coverage.is_coverage_note_dedup_conflict(caught.value), (
        "write_coverage_note recognises the violation by message to refresh instead of 500; if this "
        f"stops matching, a lost race becomes a 500. Got: {caught.value.orig!r}"
    )


def test_the_same_job_may_be_noted_in_two_different_engagements(app, session_factory):
    """The index is on the PAIR. The same job acknowledged into two report boards is ordinary."""
    with session_factory() as db:
        first, second = _board(db), _board(db)
        db.add(fm.BoardFinding(engagement_id=first.id, title="n", source_job_id="job-1"))
        db.add(fm.BoardFinding(engagement_id=second.id, title="n", source_job_id="job-1"))
        db.commit()  # must not raise


def test_authored_rows_share_a_null_source_job_id_freely(app, session_factory):
    """Partial on non-null: every row that is not a coverage note leaves `source_job_id` NULL, and many
    live in one engagement. A total unique index would refuse the second one."""
    with session_factory() as db:
        board = _board(db)
        for i in range(3):
            db.add(fm.BoardFinding(engagement_id=board.id, title=f"authored {i}"))
        db.commit()  # must not raise
        rows = db.query(fm.BoardFinding).filter_by(engagement_id=board.id).count()
    assert rows == 3


def test_an_unrelated_integrity_error_is_never_retried():
    """Refreshing is only correct for the conflict it can resolve by re-reading. Swallowing any
    `IntegrityError` would hide a real defect behind a silent refresh."""
    assert not coverage.is_coverage_note_dedup_conflict(
        IntegrityError("INSERT …", {}, Exception("FOREIGN KEY constraint failed"))
    )
    assert not coverage.is_coverage_note_dedup_conflict(
        IntegrityError("INSERT …", {}, Exception(
            "UNIQUE constraint failed: scribble_findings.engagement_id, "
            "scribble_findings.source_finding_id"
        ))
    )
    assert coverage.is_coverage_note_dedup_conflict(
        IntegrityError("INSERT …", {}, Exception(
            f'duplicate key value violates unique constraint "{fm.COVERAGE_NOTE_DEDUP_INDEX}"'
        ))
    )


def test_a_sequential_reacknowledge_still_refreshes_one_note(app, session_factory):
    """The ordinary idempotence: re-acknowledging the same job refreshes its one note, keyed on the
    column now, not the title."""
    with session_factory() as db:
        board = _board(db)
        first = coverage.write_coverage_note(
            db, engagement=board, job=_JOB, job_id="job-1", actor_username="a"
        )
        db.commit()
        again = coverage.write_coverage_note(
            db, engagement=board, job=_JOB, job_id="job-1", actor_username="a"
        )
        db.commit()
        assert first.id == again.id
        notes = db.query(fm.BoardFinding).filter_by(engagement_id=board.id).all()
    assert len(notes) == 1 and notes[0].source_job_id == "job-1"


def test_the_loser_of_a_real_race_refreshes_instead_of_duplicating(app, session_factory):
    """THE regression. Two live sessions, interleaved so both read the engagement before either commits.

    Without the fix both insert and the engagement carries the coverage note twice. With it, the loser's
    flush is refused by the index, `write_coverage_note` re-reads and refreshes the winner's note -- the
    same answer a sequential re-acknowledge gives -- leaving exactly one.
    """
    with session_factory() as db:
        board_id = _board(db).id

    winner, loser = session_factory(), session_factory()
    try:
        first = winner.get(fm.ReportBoard, board_id)
        second = loser.get(fm.ReportBoard, board_id)
        # Both read the (empty) engagement BEFORE either commits — the window the Python dedup cannot close.
        assert len(first.findings) == 0 and len(second.findings) == 0

        coverage.write_coverage_note(winner, engagement=first, job=_JOB, job_id="job-1", actor_username="a")
        winner.commit()

        coverage.write_coverage_note(loser, engagement=second, job=_JOB, job_id="job-1", actor_username="b")
        loser.commit()  # must not raise — the loser refreshes the winner's note instead of inserting
    finally:
        winner.close()
        loser.close()

    with session_factory() as db:
        notes = db.query(fm.BoardFinding).filter_by(engagement_id=board_id).all()
    assert len(notes) == 1, f"a raced double-acknowledge must leave 1 coverage note, not {len(notes)}"
    assert notes[0].source_job_id == "job-1"
