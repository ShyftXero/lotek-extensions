"""One scan finding pours into a report board AT MOST ONCE, even under concurrency (ext#257).

`promote.promote_job` dedups a re-promotion by reading `engagement.findings` into Python sets and then
inserting. Two CONCURRENT promotions of the same job into the same board -- a double-clicked "Add to
report", a client retry, the browser and the machine API at once -- both pass that read before either
commits, so before this change both inserted and the deliverable carried every promoted finding twice.

Three things have to hold, and each test below neutralizes exactly one of them:

1. The DATABASE refuses the duplicate. A Python guard cannot close a read-then-write window; only a
   constraint can, so the partial-unique index has to actually exist on a database the product builds --
   which for a fresh deployment (and every unit test) means the MODEL, since `run_migrations` builds
   fresh schemas with `create_all` and stamps head without replaying the chain.
2. Authored rows stay legal. The index is partial on non-null `source_finding_id` precisely so that
   author-written findings, synthesized parent write-ups and the #656 coverage note -- all NULL there,
   many per engagement -- are not collateral damage.
3. The LOSER of the race answers like a sequential re-promote: `skipped`, not a 500 and not a duplicate.

`test_the_loser_of_a_real_race_skips_instead_of_duplicating` drives a genuine interleaving through two
live sessions against the same file-backed SQLite database, in the exact order the race needs: both read
before either commits. It is the one that fails loudly without the fix.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import scribble.models as fm
import scribble.promote as promote
from tests.conftest import FakeFindingDTO

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "scribble/migrations/versions/a1b7c3e05d94_findings_engagement_source_unique.py"
)


def _board(db) -> fm.ReportBoard:
    board = fm.ReportBoard(name="ext257 board", scope_type="internal")
    db.add(board)
    db.commit()
    return board


def test_a_fresh_database_carries_the_partial_unique_index(app, session_factory):
    """The index must be on the MODEL, not only in the alembic revision.

    `db.run_migrations` builds a fresh database from `Base.metadata.create_all` and stamps head WITHOUT
    replaying the chain, so a migration-only index is absent from every new deployment. Its sibling
    `uq_scribble_report_board_core_engagement` is migration-only and is, in fact, missing here -- this
    test exists so this constraint does not join it.
    """
    from sqlalchemy import inspect

    with session_factory() as db:
        insp = inspect(db.get_bind())
        names = {i["name"] for i in insp.get_indexes("scribble_findings")}
    assert fm.PROMOTE_DEDUP_INDEX in names, (
        f"{fm.PROMOTE_DEDUP_INDEX} must exist on a database the product actually builds; "
        f"got {sorted(names)}"
    )


def test_the_migration_and_the_model_name_the_same_index():
    """The alembic revision repeats the literal on purpose (a migration is pinned to schema-as-it-was),
    so nothing but this assertion stops the two from drifting apart -- and a drifted pair means an
    existing deployment gets an index `is_promote_dedup_conflict` cannot recognise."""
    assert fm.PROMOTE_DEDUP_INDEX in _MIGRATION.read_text(), (
        f"{_MIGRATION.name} must build the index the model declares ({fm.PROMOTE_DEDUP_INDEX})"
    )


def test_the_index_refuses_a_duplicate_source_finding_in_one_engagement(app, session_factory):
    """The constraint bites at the DB level -- the claim every other test here rests on."""
    with session_factory() as db:
        board = _board(db)
        source_id = uuid.uuid7()
        db.add(fm.BoardFinding(engagement_id=board.id, title="a", source_finding_id=source_id))
        db.commit()
        db.add(fm.BoardFinding(engagement_id=board.id, title="a (dup)", source_finding_id=source_id))
        with pytest.raises(IntegrityError) as caught:
            db.commit()
    assert promote.is_promote_dedup_conflict(caught.value), (
        "promote_job recognises the violation by message; if this stops matching, a lost race becomes "
        f"a 500 instead of a skip. Got: {caught.value.orig!r}"
    )


def test_the_same_source_finding_may_live_in_two_different_engagements(app, session_factory):
    """The index is on the PAIR. Two report boards promoting the same core finding is ordinary."""
    with session_factory() as db:
        first, second = _board(db), _board(db)
        source_id = uuid.uuid7()
        db.add(fm.BoardFinding(engagement_id=first.id, title="a", source_finding_id=source_id))
        db.add(fm.BoardFinding(engagement_id=second.id, title="a", source_finding_id=source_id))
        db.commit()  # must not raise


def test_authored_rows_share_a_null_source_finding_id_freely(app, session_factory):
    """Partial on non-null: authored findings, parent write-ups and the #656 coverage note all carry
    NULL there, and many live in one engagement. A total unique index would refuse the second one."""
    with session_factory() as db:
        board = _board(db)
        for i in range(3):
            db.add(fm.BoardFinding(engagement_id=board.id, title=f"authored {i}"))
        db.commit()  # must not raise
        rows = db.query(fm.BoardFinding).filter_by(engagement_id=board.id).count()
    assert rows == 3


def test_the_loser_of_a_real_race_skips_instead_of_duplicating(app, session_factory):
    """THE regression. Two live sessions, interleaved so both read before either commits.

    Without the fix both insert and the board ends up with two copies of every finding. With it, the
    loser's flush is refused by the index, `promote_job` re-reads and the ordinary dedup reports the
    winner's rows as `skipped` -- the same answer a sequential re-promote gives.
    """
    with session_factory() as db:
        board = _board(db)
        board_id, core_id = board.id, board.core_engagement_id

    dtos = [
        FakeFindingDTO(id=uuid.uuid7(), title="Race finding 1", source="nmap", target_host="10.0.0.1"),
        FakeFindingDTO(id=uuid.uuid7(), title="Race finding 2", source="nmap", target_host="10.0.0.2"),
    ]

    winner, loser = session_factory(), session_factory()
    try:
        first = winner.get(fm.ReportBoard, board_id)
        second = loser.get(fm.ReportBoard, board_id)
        # Both read the (empty) board BEFORE either commits — the window the Python dedup cannot close.
        assert len(first.findings) == 0 and len(second.findings) == 0

        won = promote.promote_job(
            winner, engagement=first, findings=dtos, actor_username="a", job_engagement_id=core_id
        )
        winner.commit()
        assert won["promoted"] == 2

        lost = promote.promote_job(
            loser, engagement=second, findings=dtos, actor_username="b", job_engagement_id=core_id
        )
        loser.commit()
    finally:
        winner.close()
        loser.close()

    assert lost == {"promoted": 0, "skipped": 2, "parents": 0}, (
        f"the loser must skip what the winner already promoted; got {lost}"
    )

    with session_factory() as db:
        rows = db.query(fm.BoardFinding).filter_by(engagement_id=board_id).all()
        by_source = [r.source_finding_id for r in rows if r.source_finding_id is not None]
    assert len(rows) == 2, f"a raced double-promote must leave 2 findings, not {len(rows)}"
    assert len(set(by_source)) == 2, "each scan finding appears exactly once"


def test_an_unrelated_integrity_error_is_never_retried():
    """The retry is only correct for the conflict it can resolve by re-reading. Swallowing any
    `IntegrityError` would hide a real defect behind a silent second attempt."""
    assert not promote.is_promote_dedup_conflict(
        IntegrityError("INSERT …", {}, Exception("FOREIGN KEY constraint failed"))
    )
    assert not promote.is_promote_dedup_conflict(
        IntegrityError("INSERT …", {}, Exception("UNIQUE constraint failed: scribble_clients.name"))
    )
    assert promote.is_promote_dedup_conflict(
        IntegrityError("INSERT …", {}, Exception(
            f'duplicate key value violates unique constraint "{fm.PROMOTE_DEDUP_INDEX}"'
        ))
    )
