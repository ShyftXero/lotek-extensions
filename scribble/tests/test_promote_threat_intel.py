"""promote_job wires the consent-gated threat_intel enrichment (ext#247 P1).

The driver's own consent/audit/degradation properties are pinned in ``test_threat_intel_driver.py``;
this file pins only that ``promote_job`` ACTUALLY calls it on the rows it creates, and that the consent
gate (owned by the driver) still governs when promotion is the trigger:

  * consent ON + a mounted feed -> a promoted finding with a CVE gets its ``threat_intel`` snapshot.
  * consent OFF -> promotion populates nothing (no egress just because a finding was promoted).
  * a finding with no CVE is never looked up.
"""
from __future__ import annotations

import uuid

from scribble.models import ReportBoard
from scribble.promote import promote_job
from tests.conftest import FakeFindingDTO


def _recording_hook(feed, health):
    calls: list[list[str]] = []

    def hook(cves):
        calls.append(list(cves))
        return feed, health

    hook.calls = calls  # type: ignore[attr-defined]
    return hook


def _board(db, *, consent: bool) -> tuple[ReportBoard, uuid.UUID]:
    anchor = uuid.uuid7()
    eng = ReportBoard(name="e247", scope_type="external", core_engagement_id=anchor,
                      threat_intel_egress_consent=consent)
    db.add(eng)
    db.flush()
    return eng, anchor


def test_promote_populates_threat_intel_when_consent_on(app, stub_host, session_factory):
    hook = _recording_hook(
        {"CVE-2021-44228": {"kev": True, "kev_date_added": "2021-12-10", "epss": 0.975}},
        {"kev": "ok", "epss": "ok"},
    )
    with app.app_context():
        app.extensions["scribble"].extras["verdicts_for_cves"] = hook
        with session_factory() as db:
            eng, anchor = _board(db, consent=True)
            eid = eng.id
            promote_job(
                db, engagement=eng,
                findings=[FakeFindingDTO(id=uuid.uuid7(), title="log4shell", cve="CVE-2021-44228")],
                actor_username="op", job_engagement_id=anchor,
            )
            db.commit()
        with session_factory() as db:
            eng = db.get(ReportBoard, eid)
            [f] = eng.findings
            ti = f.threat_intel
    assert hook.calls == [["CVE-2021-44228"]]           # promotion drove the lookup
    assert ti["cves"]["CVE-2021-44228"]["kev"] is True  # and the snapshot landed on the row


def test_promote_populates_nothing_when_consent_off(app, stub_host, session_factory):
    hook = _recording_hook({"CVE-2021-44228": {"kev": True}}, {"kev": "ok"})
    with app.app_context():
        app.extensions["scribble"].extras["verdicts_for_cves"] = hook
        with session_factory() as db:
            eng, anchor = _board(db, consent=False)  # default OFF
            eid = eng.id
            promote_job(
                db, engagement=eng,
                findings=[FakeFindingDTO(id=uuid.uuid7(), title="log4shell", cve="CVE-2021-44228")],
                actor_username="op", job_engagement_id=anchor,
            )
            db.commit()
        with session_factory() as db:
            [f] = db.get(ReportBoard, eid).findings
            assert f.threat_intel is None
    assert hook.calls == []  # consent off -> promotion attempts no egress


def test_promote_skips_lookup_for_finding_without_cve(app, stub_host, session_factory):
    hook = _recording_hook({"CVE-1": {"kev": True}}, {"kev": "ok"})
    with app.app_context():
        app.extensions["scribble"].extras["verdicts_for_cves"] = hook
        with session_factory() as db:
            eng, anchor = _board(db, consent=True)
            promote_job(
                db, engagement=eng,
                findings=[FakeFindingDTO(id=uuid.uuid7(), title="info leak")],  # no cve
                actor_username="op", job_engagement_id=anchor,
            )
            db.commit()
    assert hook.calls == []  # nothing to look up
