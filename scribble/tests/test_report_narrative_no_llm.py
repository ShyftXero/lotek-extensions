"""LOT-48/4: the templated, no-LLM prose module (``scribble.reporting.narrative``).

Pins the three acceptance criteria of LOT-72:
  1. The report builds with ZERO model calls by default.
  2. The executive summary never reports "no issues" for a job that did not run.
  3. The AI-assist flag defaults OFF, and with the provider disabled the narrative is byte-for-byte
     identical to the deterministic string.

Plus the mechanics: the Jinja exec-summary template states tested / found / could-not-run in that
order and counts discovered attack paths, and the per-finding one-liner renders deterministically over
``FindingCtx`` fields.
"""

from __future__ import annotations

from types import SimpleNamespace

from scribble import coverage
from scribble.enums import Severity
from scribble.models import BoardFinding, Client, FindingGroup, ReportBoard
from scribble.reporting import build_report_context
from scribble.reporting import context as context_mod
from scribble.reporting import narrative as N
from scribble.reporting.context import FindingCtx, SeverityRollup


def _roll(**counts) -> SeverityRollup:
    base = {s.value: 0 for s in Severity}
    base.update(counts)
    return SeverityRollup(counts=base, total=sum(base.values()), overall="x")


def _fc(**over) -> FindingCtx:
    d = dict(
        id=1, title="", severity="", cvss_score=None, cvss_vector=None, target_host=None,
        target_port=None, target_url=None, blocks_html={}, artifacts=[],
    )
    d.update(over)
    return FindingCtx(**d)


# --------------------------------------------------------------------------- exec summary honesty

def test_exec_summary_ran_clean_branch_when_no_coverage_gap():
    """RAN-CLEAN (Ghostwriter branch B): assessed, nothing found, no gap. The "no issues" wording lives
    only here, and it is paired with the honest hedge that it is not a clean bill for what was not tested."""
    out = N.render_executive_summary(company_name="Acme", rollup=_roll(), top_titles=[])
    assert out == (
        "We assessed Acme and found no issues in the tested scope. That is a clean result for what we "
        "tested, not a clean bill for what we did not."
    )


def test_exec_summary_never_says_no_issues_for_a_job_that_did_not_run():
    """AC2. Zero findings BUT a scan job could not be assessed -> the COULD-NOT-RUN branch (Ghostwriter C):
    it must state the could-not-run posture and must NOT read as an all-clear. The RAN-CLEAN "no issues"
    wording must be unreachable here."""
    out = N.render_executive_summary(
        company_name="Acme", rollup=_roll(), top_titles=[], coverage_limited_jobs=1
    )
    lowered = out.lower()
    assert "found no issues in the tested scope" not in lowered  # the RAN-CLEAN wording is unreachable
    assert "we could not complete the assessment" in lowered
    assert "1 scan job could not be assessed" in lowered
    assert "does not mean the rest is clean. it means we did not test it" in lowered  # the honest hedge


def test_exec_summary_states_tested_found_couldnotrun_in_order_with_paths():
    """MIXED (Ghostwriter branch D): findings AND a coverage gap. Found (count, severity breakdown,
    paths) is stated before the could-not-run disclaimer, in that order."""
    out = N.render_executive_summary(
        company_name="Acme Corp",
        rollup=_roll(critical=2, high=1, low=3),
        top_titles=["Domain Admin Compromise", "SMB signing not required"],
        path_count=2,
        coverage_limited_jobs=1,
    )
    assert "found 6 issues" in out
    assert "2 critical, 1 high, and 3 low" in out  # every nonzero band, most severe first
    # found: cradle-to-DA path count, plural phrasing
    assert "We chained 2 separate paths from a starting foothold to Domain Admin." in out
    # ordering: found (paths) precedes could-not-run (coverage disclaimer)
    assert "We could not assess 1 scan job. This report makes no claim about that scope." in out
    assert out.index("We chained 2") < out.index("We could not assess")
    assert "We could not assess 1 scan job could not be assessed" not in out  # noun phrase, not doubled


def test_exec_summary_singular_and_no_high_crit_branch():
    """FOUND (branch A) with only low-severity findings: the count and severity breakdown are stated, and
    no highest-risk sentence is emitted (there is no LIVE crit/high to name)."""
    out = N.render_executive_summary(company_name="Acme", rollup=_roll(low=1), top_titles=[])
    assert out == "We assessed Acme and found 1 issue: 1 low."
    assert "highest-risk findings" not in out


def test_no_em_dashes_in_generated_prose():
    """House style (AGENTS doctrine): no em dashes in any generated string."""
    samples = [
        N.render_executive_summary(
            company_name="Acme", rollup=_roll(), top_titles=[], coverage_limited_jobs=2
        ),
        N.render_executive_summary(
            company_name="Acme", rollup=_roll(critical=1), top_titles=["X"], path_count=1
        ),
        N.render_finding_one_liner(_fc(title="T", severity="high", target_host="h", target_port="443",
                                       cve_ids=["CVE-1"], threat_intel={"kev": True})),
    ]
    for s in samples:
        assert "—" not in s and "–" not in s


# --------------------------------------------------------------------------- per-finding one-liner

def test_one_liner_full_shape():
    out = N.render_finding_one_liner(_fc(
        title="SMB signing not required", severity="high", target_host="10.0.0.5",
        target_port="445", cve_ids=["CVE-2020-1472"], threat_intel={"kev": True},
    ))
    assert out == ("High severity on 10.0.0.5:445, CVE-2020-1472. "
                   "Listed in CISA KEV as known exploited.")


def test_one_liner_multiple_cves():
    out = N.render_finding_one_liner(_fc(
        title="Chained CVEs", severity="critical", target_host="10.0.0.9",
        cve_ids=["CVE-2021-1", "CVE-2021-2"],
    ))
    assert out == "Critical severity on 10.0.0.9, CVE-2021-1 and CVE-2021-2."


def test_one_liner_info_word_and_scope_floor():
    """info -> "Informational" (Ghostwriter), and a finding with no host/url falls back to "the tested
    scope" so the sentence never dangles. No title, no CVE clause, no KEV sentence."""
    out = N.render_finding_one_liner(_fc(title="Info leak", severity="info"))
    assert out == "Informational severity on the tested scope."


def test_one_liner_prefers_url_when_no_host():
    out = N.render_finding_one_liner(_fc(title="Reflected XSS", severity="medium",
                                         target_url="https://app/x"))
    assert out == "Medium severity on https://app/x."


def test_one_liner_is_deterministic():
    f = _fc(title="T", severity="low", target_host="h")
    assert N.render_finding_one_liner(f) == N.render_finding_one_liner(f)


# --------------------------------------------------------------------------- AI-assist seam (off)

def test_polish_is_identity_by_default():
    assert N.polish("keep me exactly", kind="exec_summary") == "keep me exactly"


def test_polish_does_not_call_provider_when_flag_off():
    """AC1/AC3. A provider hook may be present, but with the flag off it is NEVER invoked and the text is
    returned byte-for-byte."""
    def landmine(*a, **k):
        raise AssertionError("model must not be called on the default path")

    # enable defaults off -> ai_hook is never consulted
    assert N.polish("text", kind="exec_summary", ai_hook=landmine) == "text"


def test_polish_fails_closed_to_input():
    def landmine(*a, **k):
        raise RuntimeError("provider blew up")

    # explicitly enabled + a provider that raises -> still returns the deterministic input unchanged
    assert N.polish("deterministic", kind="exec_summary", enable=True, ai_hook=landmine) == "deterministic"
    # explicitly enabled but no provider -> input unchanged
    assert N.polish("deterministic", kind="exec_summary", enable=True, ai_hook=None) == "deterministic"


def test_ai_polish_disabled_without_env_flag(monkeypatch):
    monkeypatch.delenv(N._AI_POLISH_ENV, raising=False)
    assert N.ai_polish_enabled() is False


# --------------------------------------------------------------------------- end-to-end build

def _clean_engagement(db, *, company="Acme Corp"):
    c = Client(name="Acme")
    db.add(c)
    db.flush()
    eng = ReportBoard(name="Eng", client_id=c.id, company_name=company)
    db.add(eng)
    return eng


def test_report_builds_with_zero_model_calls_by_default(session_factory, monkeypatch):
    """AC1. Build the report with an AI hook armed as a landmine and the flag off: the build completes,
    the narrative is populated, and the ``ai_stream`` hook is never even requested."""
    requested: list[str] = []
    real_hook = context_mod.host.host_hook

    def recording_hook(name):
        requested.append(name)
        if name == N._AI_HOOK:
            def _landmine(*a, **k):
                raise AssertionError("model called during a default report build")
            return _landmine
        return real_hook(name)

    monkeypatch.setattr(N.host, "host_hook", recording_hook)
    monkeypatch.delenv(N._AI_POLISH_ENV, raising=False)

    with session_factory() as db:
        eng = _clean_engagement(db)
        g = FindingGroup(engagement=eng, name="Internal", order_index=0)
        db.add(BoardFinding(engagement=eng, group=g, title="Domain Admin Compromise",
                            severity=Severity.critical, order_index=0))
        db.commit()
        ctx = build_report_context(db.get(ReportBoard, eng.id))

    assert ctx.narrative
    assert "Domain Admin Compromise" in ctx.narrative
    assert N._AI_HOOK not in requested


def test_build_narrative_matches_deterministic_render(session_factory):
    """AC3. With the flag off, ``ctx.narrative`` equals the direct deterministic render -- proving the
    polish seam did not touch it -- and two builds are byte-identical."""
    with session_factory() as db:
        eng = _clean_engagement(db)
        g = FindingGroup(engagement=eng, name="Internal", order_index=0)
        db.add(BoardFinding(engagement=eng, group=g, title="Weak SMB Signing",
                            severity=Severity.high, order_index=0))
        db.commit()
        ctx_a = build_report_context(db.get(ReportBoard, eng.id))
        ctx_b = build_report_context(db.get(ReportBoard, eng.id))

    expected = N.render_executive_summary(
        company_name="Acme Corp",
        rollup=ctx_a.rollup,
        top_titles=["Weak SMB Signing"],
    )
    assert ctx_a.narrative == expected
    assert ctx_a.narrative == ctx_b.narrative


def test_build_populates_finding_one_liner(session_factory):
    with session_factory() as db:
        eng = _clean_engagement(db)
        g = FindingGroup(engagement=eng, name="Internal", order_index=0)
        db.add(BoardFinding(engagement=eng, group=g, title="Weak SMB Signing",
                            severity=Severity.high, order_index=0, target_host="10.0.0.5"))
        db.commit()
        ctx = build_report_context(db.get(ReportBoard, eng.id))

    f = ctx.groups[0].findings[0]
    assert f.one_liner == "High severity on 10.0.0.5."


def test_exec_summary_honesty_end_to_end_with_coverage_note(session_factory):
    """AC2 end-to-end. An engagement whose only artifact is an acknowledged scan-coverage limitation
    (``coverage.write_coverage_note``) must not read as an all-clear."""
    with session_factory() as db:
        eng = _clean_engagement(db)
        db.flush()
        job = SimpleNamespace(assessed=False, unassessed_modules=("nuclei", "nmap"))
        coverage.write_coverage_note(
            db, engagement=eng, job=job, job_id="job-123", actor_username="op"
        )
        db.commit()
        # Production builds the report from a fresh engagement load; expire so the note is read back
        # into the relationship (the test session is expire_on_commit=False).
        db.expire_all()
        ctx = build_report_context(db.get(ReportBoard, eng.id))

    # The coverage note is itself a report-visible info finding, so this engagement reads as MIXED
    # (found + a gap), never as an all-clear. AC2: the could-not-run posture is stated and "no issues" is
    # unreachable.
    lowered = ctx.narrative.lower()
    assert "found no issues in the tested scope" not in lowered
    assert "we could not assess" in lowered
    assert "makes no claim about that scope" in lowered
