"""``scribble.enrichment`` propose/decide primitives + the CVSS->severity band (ext#247 P1).

The threat_intel DRIVER is covered by ``test_threat_intel_driver.py``; this file pins the severity-
PROPOSAL seam the roadmap's review flagged as unfinished scaffolding:

  * ``severity_from_cvss`` \u2014 the pure CVSS-band derivation that REPLACES the retired product/version ABC.
  * ``propose`` is IDEMPOTENT \u2014 one pending row per ``(finding_id, source)``, never a stacked duplicate.
  * ``decide(accept=True)`` APPLIES the promotion (sets the matching ``BoardFinding.risk_override`` +
    rationale) and AUDITS it \u2014 the "owed follow-on (INV-AUDIT-04)" the old no-op ``decide`` left undone.
  * ``decide(accept=False)`` applies nothing.
  * the apply is TENANT-SCOPED \u2014 it only touches a finding in the proposal's own engagement.
"""
from __future__ import annotations

import uuid

from scribble.enrichment import (
    EnrichmentStatus,
    decide,
    propose,
    severity_from_cvss,
)
from scribble.enums import Severity
from scribble.models import BoardFinding, ReportBoard

# -- the CVSS band (replaces the product/version ABC) --

def test_severity_from_cvss_bands():
    assert severity_from_cvss(0.0) is Severity.info
    assert severity_from_cvss(3.9) is Severity.low
    assert severity_from_cvss(4.0) is Severity.medium
    assert severity_from_cvss(6.9) is Severity.medium
    assert severity_from_cvss(7.0) is Severity.high
    assert severity_from_cvss(8.9) is Severity.high
    assert severity_from_cvss(9.0) is Severity.critical
    assert severity_from_cvss(10.0) is Severity.critical


def test_severity_from_cvss_rejects_unusable_scores():
    assert severity_from_cvss(None) is None
    assert severity_from_cvss(True) is None   # a bool is not a score
    assert severity_from_cvss(-1) is None
    assert severity_from_cvss(10.1) is None
    assert severity_from_cvss("nope") is None  # type: ignore[arg-type]


# -- helpers --

def _engagement(db, **kw) -> ReportBoard:
    eng = ReportBoard(name="e247", scope_type="external", **kw)
    db.add(eng)
    db.flush()
    return eng


def _finding(db, eng, *, source_finding_id, severity="medium") -> BoardFinding:
    f = BoardFinding(engagement_id=eng.id, title="rce", severity=severity,
                     source_finding_id=source_finding_id)
    db.add(f)
    db.flush()
    return f


# -- propose idempotency --

def test_propose_is_promote_only(session_factory):
    with session_factory() as db:
        eng = _engagement(db)
        fid = uuid.uuid7()
        # suggested <= current is not a promotion -> no row
        assert propose(db, finding_id=fid, engagement_id=eng.id, current=Severity.high,
                       suggested=Severity.low, reason="x", source="s") is None
        assert propose(db, finding_id=fid, engagement_id=eng.id, current=Severity.high,
                       suggested=Severity.high, reason="x", source="s") is None


def test_propose_is_idempotent_per_finding_and_source(session_factory):
    with session_factory() as db:
        eng = _engagement(db)
        fid = uuid.uuid7()
        first = propose(db, finding_id=fid, engagement_id=eng.id, current=Severity.medium,
                        suggested=Severity.high, reason="cvss 7.5", source="src-a")
        again = propose(db, finding_id=fid, engagement_id=eng.id, current=Severity.medium,
                        suggested=Severity.critical, reason="cvss 9.1", source="src-a")
        assert first is not None
        assert again is first  # the pending row is returned, NOT a second stacked proposal
        # a DIFFERENT source may stand its own proposal for the same finding
        other = propose(db, finding_id=fid, engagement_id=eng.id, current=Severity.medium,
                        suggested=Severity.high, reason="y", source="src-b")
        assert other is not None and other is not first


def test_propose_again_after_decision_is_a_fresh_row(session_factory):
    with session_factory() as db:
        eng = _engagement(db)
        fid = uuid.uuid7()
        first = propose(db, finding_id=fid, engagement_id=eng.id, current=Severity.medium,
                        suggested=Severity.high, reason="r", source="s")
        decide(db, first, accept=False, decided_by=None)  # no longer pending
        fresh = propose(db, finding_id=fid, engagement_id=eng.id, current=Severity.medium,
                        suggested=Severity.high, reason="r2", source="s")
        assert fresh is not None and fresh is not first


# -- decide: accept applies the override + audits --

def test_decide_accept_applies_severity_and_audits(app, stub_host, session_factory):
    with app.app_context():
        with session_factory() as db:
            eng = _engagement(db)
            core_fid = uuid.uuid7()
            finding = _finding(db, eng, source_finding_id=core_fid, severity="medium")
            p = propose(db, finding_id=core_fid, engagement_id=eng.id, current=Severity.medium,
                        suggested=Severity.high, reason="tridentstack CVSS 7.5", source="tridentstack")
            fid = finding.id
            decide(db, p, accept=True, decided_by=None)
            db.commit()
        with session_factory() as db:
            f = db.get(BoardFinding, fid)
            assert f.severity is Severity.high  # the rendered severity moved (review's "reader sees it")
    # the decision is audited through the host seam, with the original severity preserved in `before`
    assert len(stub_host.audit_calls) == 1
    action, kw = stub_host.audit_calls[0]
    assert action == "ext:scribble:enrichment_accepted"
    assert kw["before"]["severity"] == "medium"
    assert kw["after"]["severity"] == "high"
    assert kw["after"]["source"] == "tridentstack"


def test_decide_reject_applies_nothing(app, stub_host, session_factory):
    with app.app_context():
        with session_factory() as db:
            eng = _engagement(db)
            core_fid = uuid.uuid7()
            finding = _finding(db, eng, source_finding_id=core_fid, severity="medium")
            p = propose(db, finding_id=core_fid, engagement_id=eng.id, current=Severity.medium,
                        suggested=Severity.high, reason="r", source="s")
            fid = finding.id
            decide(db, p, accept=False, decided_by=None)
            assert p.status == EnrichmentStatus.rejected
            db.commit()
        with session_factory() as db:
            f = db.get(BoardFinding, fid)
            assert f.severity is Severity.medium  # unchanged
    assert stub_host.audit_calls == []  # a reject touches nothing -> nothing to audit


def test_decide_accept_is_tenant_scoped(app, stub_host, session_factory):
    """The apply only touches a finding in the proposal's OWN engagement \u2014 a same-core-id finding on a
    DIFFERENT engagement is never overridden."""
    with app.app_context():
        with session_factory() as db:
            eng_a = _engagement(db)
            eng_b = _engagement(db)
            core_fid = uuid.uuid7()
            # same core finding id poured onto BOTH boards (a finding visible in two engagements)
            fa = _finding(db, eng_a, source_finding_id=core_fid, severity="medium")
            fb = _finding(db, eng_b, source_finding_id=core_fid, severity="medium")
            p = propose(db, finding_id=core_fid, engagement_id=eng_a.id, current=Severity.medium,
                        suggested=Severity.high, reason="r", source="s")
            fa_id, fb_id = fa.id, fb.id
            decide(db, p, accept=True, decided_by=None)
            db.commit()
        with session_factory() as db:
            assert db.get(BoardFinding, fa_id).severity is Severity.high
            assert db.get(BoardFinding, fb_id).severity is Severity.medium  # other tenant untouched
