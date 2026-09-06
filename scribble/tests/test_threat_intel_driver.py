"""``scribble.enrichment.ThreatIntelDriver`` + ``egress_consented`` — the #642 threat_intel (KEV/EPSS)
enrichment driver (SCAFFOLDING; the live feed is core-side STUBBED, no egress).

The driver PRODUCES the ``feed`` from the host ``verdicts_for_cves`` hook and calls the already-existing
``metadata.build_threat_intel`` — it does not reimplement the snapshot shape. The properties under test
are the SECURITY plumbing around that call:

  * CONSENT (INV-EGRESS-02): a per-engagement flag, default OFF, FORCED OFF for internal engagements.
    OFF ⇒ NO lookup (the hook is never called) and NO audit row.
  * AUDIT (INV-EGRESS-02): one audit row per LOOKUP (consent ON + hook called) carrying the engagement,
    the driver, the CVE count and the health map.
  * DEGRADATION (INV-DEPLOY-02 / INV-DATA-07): a degraded/empty feed leaves ``threat_intel`` None — a
    degraded control never reads as a clean "nothing exploitable" — and the audit records the health.

EDD: the ``feed`` is a HAND-WRITTEN synthetic INPUT, never a captured exploiteer response. Red-before:
``ThreatIntelDriver`` / ``egress_consented`` did not exist (ImportError). Green-after: this file.
"""
from __future__ import annotations

from scribble.enrichment import ThreatIntelDriver, egress_consented
from scribble.models import Engagement, EngagementFinding


def _recording_hook(feed, health):
    """A synthetic ``verdicts_for_cves`` hook that records the CVEs it was asked about, so a test can
    prove a lookup did (or did NOT) happen."""
    calls: list[list[str]] = []

    def hook(cves):
        calls.append(list(cves))
        return feed, health

    hook.calls = calls  # type: ignore[attr-defined]
    return hook


def _make_finding(session_factory, *, scope_type="external", consent=False,
                  cve_ids=("CVE-2021-44228",)):
    with session_factory() as db:
        eng = Engagement(name="e642", scope_type=scope_type, threat_intel_egress_consent=consent)
        db.add(eng)
        db.flush()
        finding = EngagementFinding(engagement_id=eng.id, title="rce", severity="high",
                                    cve_ids=list(cve_ids))
        db.add(finding)
        db.commit()
        return finding.id


# ── the ONE consent predicate ──────────────────────────────────────────────────────────────────────

def test_egress_consented_is_off_by_default(session_factory):
    with session_factory() as db:
        eng = Engagement(name="d", scope_type="external")
        db.add(eng)
        db.flush()
        assert egress_consented(eng) is False


def test_egress_consented_true_when_external_and_flag_set(session_factory):
    with session_factory() as db:
        eng = Engagement(name="c", scope_type="external", threat_intel_egress_consent=True)
        db.add(eng)
        db.flush()
        assert egress_consented(eng) is True


def test_egress_consented_forced_off_for_internal_even_with_flag(session_factory):
    with session_factory() as db:
        eng = Engagement(name="i", scope_type="internal", threat_intel_egress_consent=True)
        db.add(eng)
        db.flush()
        assert egress_consented(eng) is False


# ── the driver ───────────────────────────────────────────────────────────────────────────────────--

def test_a_consent_on_with_mock_feed_populates_threat_intel(app, stub_host, session_factory):
    fid = _make_finding(session_factory, consent=True)
    hook = _recording_hook(
        {"CVE-2021-44228": {"kev": True, "kev_date_added": "2021-12-10", "epss": 0.975}},
        {"kev": "ok", "epss": "ok"},
    )
    with app.app_context():
        app.extensions["scribble"].extras["verdicts_for_cves"] = hook
        with session_factory() as db:
            finding = db.get(EngagementFinding, fid)
            snap = ThreatIntelDriver().propose(db, finding)
            ThreatIntelDriver().apply(db, finding, snap)
            db.commit()
        with session_factory() as db:
            ti = db.get(EngagementFinding, fid).threat_intel
    assert hook.calls == [["CVE-2021-44228"]]           # the lookup happened, with the finding's CVEs
    assert ti["source"] == "exploiteer"
    assert ti["cves"]["CVE-2021-44228"]["kev"] is True  # produced by build_threat_intel, not reinvented
    # one audit row per lookup, carrying engagement + driver + cve count + health
    assert len(stub_host.audit_calls) == 1
    action, kw = stub_host.audit_calls[0]
    assert action == "ext:scribble:threat_intel_lookup"  # the seam namespaces every scribble verb
    assert kw["after"]["health"] == {"kev": "ok", "epss": "ok"}
    assert kw["after"]["cve_count"] == 1
    assert kw["after"]["driver"] == "exploiteer"


def test_b_exploiteer_absent_yields_none_without_error_or_audit(app, stub_host, session_factory):
    fid = _make_finding(session_factory, consent=True)
    with app.app_context():
        # no verdicts_for_cves hook wired -> exploiteer unmounted -> degrade to no snapshot
        with session_factory() as db:
            finding = db.get(EngagementFinding, fid)
            assert ThreatIntelDriver().propose(db, finding) is None
    assert stub_host.audit_calls == []  # no lookup happened -> nothing to audit


def test_c_consent_off_makes_no_lookup_and_no_audit(app, stub_host, session_factory):
    fid = _make_finding(session_factory, consent=False)  # default OFF
    hook = _recording_hook({"CVE-2021-44228": {"kev": True, "epss": 0.9}}, {"kev": "ok", "epss": "ok"})
    with app.app_context():
        app.extensions["scribble"].extras["verdicts_for_cves"] = hook
        with session_factory() as db:
            finding = db.get(EngagementFinding, fid)
            assert ThreatIntelDriver().propose(db, finding) is None
    assert hook.calls == []             # consent off -> the hook is NEVER called (no egress attempt)
    assert stub_host.audit_calls == []  # and nothing is audited


def test_d_degraded_feed_leaves_threat_intel_none_but_audits_health(app, stub_host, session_factory):
    fid = _make_finding(session_factory, consent=True)
    # the core stub's shape: an empty feed + a not-wired/degraded health map (an OUTAGE, not "clean").
    hook = _recording_hook({}, {"kev": "unavailable", "epss": "unavailable"})
    with app.app_context():
        app.extensions["scribble"].extras["verdicts_for_cves"] = hook
        with session_factory() as db:
            finding = db.get(EngagementFinding, fid)
            snap = ThreatIntelDriver().propose(db, finding)
            ThreatIntelDriver().apply(db, finding, snap)
            db.commit()
        with session_factory() as db:
            ti = db.get(EngagementFinding, fid).threat_intel
    assert ti is None                          # degraded != "nothing exploitable" (INV-DEPLOY-02)
    assert hook.calls == [["CVE-2021-44228"]]  # the lookup DID fire (consent on, hook present)
    assert len(stub_host.audit_calls) == 1     # and the degraded state is recorded
    _, kw = stub_host.audit_calls[0]
    assert kw["after"]["health"] == {"kev": "unavailable", "epss": "unavailable"}


def test_wire_mock_host_injects_verdicts_hook_for_the_mounted_case(app):
    """The testbed can simulate exploiteer MOUNTED by injecting a stub feed, or ABSENT by omitting it."""
    from scribble.testing import wire_mock_host
    cfg = app.extensions["scribble"]
    hook = _recording_hook({"CVE-1": {"kev": True}}, {"kev": "ok"})
    wire_mock_host(cfg, verdicts_for_cves=hook)  # MOUNTED
    assert cfg.extras["verdicts_for_cves"] is hook
    # ABSENT: a wiring that passes nothing must not manufacture a hook.
    cfg.extras.pop("verdicts_for_cves", None)
    wire_mock_host(cfg)
    assert "verdicts_for_cves" not in cfg.extras


def test_e_internal_engagement_forces_consent_off(app, stub_host, session_factory):
    # internal engagement, consent flag explicitly ON -> STILL forced off: an internal engagement's CVEs
    # never leave the box regardless of the flag.
    fid = _make_finding(session_factory, scope_type="internal", consent=True)
    hook = _recording_hook({"CVE-2021-44228": {"kev": True, "epss": 0.9}}, {"kev": "ok", "epss": "ok"})
    with app.app_context():
        app.extensions["scribble"].extras["verdicts_for_cves"] = hook
        with session_factory() as db:
            finding = db.get(EngagementFinding, fid)
            assert ThreatIntelDriver().propose(db, finding) is None
    assert hook.calls == []
    assert stub_host.audit_calls == []
