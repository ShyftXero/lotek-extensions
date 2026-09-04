"""Retest data model (lotek#621) — the round-trip, the outcome→status writer, and the migration.

This is the DATA MODEL that feeds the retest UI/API (#622): a ``Retest`` row records one verify-the-fix
pass on a finding, ``RetestOutcome`` names its result, and ``findings_service.record_retest`` is the ONE
writer both surfaces call so the outcome→status policy lives in a single place (the "one derived-state
predicate, one home" rule). ``Artifact.retest_id`` links retest evidence back to the round it belongs to.

The migration is exercised in isolation on SQLite (the full chain can't replay on SQLite — the UUID-PK
revision is Postgres-only DDL — so ``test_alembic_adoption`` proves the real end-to-end upgrade on
Postgres and skips here). Single-head is proven separately by ``test_migration_single_head``.
"""
from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from scribble.content import schema
from scribble.enums import FindingStatus, RetestOutcome
from scribble.findings_service import record_retest
from scribble.models import Artifact, EngagementFinding, Retest, VulnerabilityTemplate


def _engagement_with_finding(db):
    import uuid

    from scribble.models import Client, Engagement

    client = Client(name=f"Retest Client {uuid.uuid4()}")  # Client.name is UNIQUE; keep each call distinct
    db.add(client)
    db.flush()
    eng = Engagement(name="Retest Engagement", client_id=client.id)
    db.add(eng)
    db.flush()
    tmpl = VulnerabilityTemplate(
        name="Stored XSS", default_severity="high",
        content_json={"description": schema.doc_from_text("XSS.")},
    )
    db.add(tmpl)
    db.flush()
    finding = EngagementFinding.from_template(tmpl, engagement_id=eng.id)
    db.add(finding)
    db.flush()
    return eng, finding


def test_retest_round_trips(session_factory):
    with session_factory() as db:
        _eng, finding = _engagement_with_finding(db)
        db.add(Retest(
            finding_id=finding.id,
            outcome=RetestOutcome.remediated,
            notes="Re-ran the payload; sanitized now.",
            tested_by="alice",
            tested_on=date(2026, 9, 4),
        ))
        db.commit()
        finding_id = finding.id

    with session_factory() as db:
        rows = db.scalars(sa.select(Retest).where(Retest.finding_id == finding_id)).all()
        assert len(rows) == 1
        r = rows[0]
        assert r.outcome is RetestOutcome.remediated
        assert r.notes == "Re-ran the payload; sanitized now."
        assert r.tested_by == "alice"
        assert r.tested_on == date(2026, 9, 4)
        # readable back through the finding relationship the report renderer walks
        assert [x.id for x in db.get(EngagementFinding, finding_id).retests] == [r.id]


def test_record_retest_transitions_status_and_appends_a_row(session_factory):
    with session_factory() as db:
        _eng, finding = _engagement_with_finding(db)
        assert finding.status is FindingStatus.new

        retest = record_retest(db, finding, RetestOutcome.remediated, tested_by="bob")
        assert retest.id is not None
        assert finding.status is FindingStatus.fixed  # a verified fix closes the finding
        db.commit()
        finding_id = finding.id

    with session_factory() as db:
        finding = db.get(EngagementFinding, finding_id)
        assert finding.status is FindingStatus.fixed
        assert len(finding.retests) == 1


def test_record_retest_outcome_status_mapping(session_factory):
    cases = {
        RetestOutcome.remediated: FindingStatus.fixed,
        RetestOutcome.partially_remediated: FindingStatus.needs_retest,
        RetestOutcome.not_remediated: FindingStatus.needs_retest,
        RetestOutcome.accepted_risk: FindingStatus.accepted_risk,
        RetestOutcome.not_tested: FindingStatus.triaged,  # unchanged from the start state below
    }
    # Exhaustive: a new RetestOutcome must decide its status here (and in _RETEST_OUTCOME_STATUS, which
    # record_retest indexes with a bare [outcome]) — otherwise the writer KeyErrors in prod, not in CI.
    assert set(cases) == set(RetestOutcome), set(RetestOutcome) - set(cases)
    for outcome, expected in cases.items():
        with session_factory() as db:
            _eng, finding = _engagement_with_finding(db)
            finding.status = FindingStatus.triaged  # a deliberate non-default start state
            db.flush()
            record_retest(db, finding, outcome)
            assert finding.status is expected, f"{outcome} -> {finding.status}, expected {expected}"
            db.commit()


def test_migration_creates_retests_table_and_artifact_link(tmp_path):
    """The migration's own DDL, applied to a SQLite DB that predates it (no scribble_retests, no
    scribble_artifacts.retest_id), then re-applied (idempotent) and reversed (downgrade)."""
    from scribble.db import Base
    from scribble.migrations.versions import a1b2c3d4e5f6_retest_model as mig

    eng = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    # Build the current schema, then strip the two things the migration adds, to simulate "before".
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(sa.text("DROP TABLE scribble_retests"))
        # SQLite refuses to drop a column that still has an index, so drop the index first.
        c.execute(sa.text("DROP INDEX ix_scribble_artifacts_retest_id"))
        c.execute(sa.text("ALTER TABLE scribble_artifacts DROP COLUMN retest_id"))

    def _run(fn):
        with eng.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()

    assert "scribble_retests" not in sa.inspect(eng).get_table_names()
    _run(mig.upgrade)
    insp = sa.inspect(eng)
    assert "scribble_retests" in insp.get_table_names()
    assert "retest_id" in {c["name"] for c in insp.get_columns("scribble_artifacts")}
    _run(mig.upgrade)  # idempotent: a second apply is a no-op, not a duplicate-table error

    _run(mig.downgrade)
    insp = sa.inspect(eng)
    assert "scribble_retests" not in insp.get_table_names()
    assert "retest_id" not in {c["name"] for c in insp.get_columns("scribble_artifacts")}


def test_artifact_carries_a_retest_link(session_factory):
    with session_factory() as db:
        _eng, finding = _engagement_with_finding(db)
        retest = record_retest(db, finding, RetestOutcome.not_remediated)
        db.flush()
        art = Artifact(
            engagement_id=finding.engagement_id,
            finding_id=finding.id,
            retest_id=retest.id,
            filename="retest.png",
            storage_path="obj:deadbeef",
        )
        db.add(art)
        db.commit()
        art_id = art.id
        retest_id = retest.id

    with session_factory() as db:
        assert db.get(Artifact, art_id).retest_id == retest_id
