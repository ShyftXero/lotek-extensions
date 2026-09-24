"""Phase 1b — the DELIVERABLE's By-vulnerability / By-host rollup block.

The board (test_board_finding_grouping.py) and the core mounted suite cover the shared bucketer + the
seam. This module covers the DELIVERABLE-specific halves:
  * the renderer emits `sec-rollups` (+ its TOC/nav entry) when the context carries buckets, and NOTHING
    when it doesn't (the byte-identical guarantee);
  * build_report_context feeds the seam only the REPORT-VISIBLE findings — the deliverable shows fewer
    findings than the board, so an excluded finding must never leak into the client rollup.
End-to-end collapse against the real bucketer is proven mounted in lotek core's suite (not importable here).
"""

from __future__ import annotations

from types import SimpleNamespace

from scribble.enums import Severity
from scribble.models import BoardFinding, Client, FindingGroup, ReportBoard
from scribble.reporting import build_report_context
from scribble.reporting.render_html import render_report_html


def _bucket(**kw) -> SimpleNamespace:
    kw.setdefault("items", [])
    return SimpleNamespace(**kw)


# --------------------------------------------------------------------------- renderer (pure)

def test_rollups_block_renders_when_buckets_present_and_omits_when_empty(session_factory):
    with session_factory() as db:
        client = Client(name="Acme")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="Q3", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Internal", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        db.add(BoardFinding(engagement_id=eng.id, group_id=grp.id, title="EternalBlue",
                            severity=Severity.critical, target_host="10.0.0.1",
                            source_facts={"dedupe_key": "nuclei:ms17-010", "source": "nuclei"}))
        db.commit()
        eng_id = eng.id
    # Reload in a fresh session (as production + the other render tests do) so timestamps come back
    # DB-consistent — build_report_context is only ever called on a reloaded board.
    with session_factory() as db:
        ctx = build_report_context(db.get(ReportBoard, eng_id))

    # Offline: the seam returned [] -> no rollup section (byte-identical to before the block existed).
    assert 'id="sec-rollups"' not in render_report_html(ctx)

    # Inject the buckets the in-request seam would supply -> the static section renders.
    it = SimpleNamespace(label="EternalBlue")
    ctx.findings_by_kind = [_bucket(label="EternalBlue", severity="critical",
                                    hosts=["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                                    cves=["CVE-2017-0144"], count=3, key="EternalBlue", items=[it])]
    ctx.findings_by_host = [_bucket(label="", severity="critical", hosts=["10.0.0.1"],
                                    cves=["CVE-2017-0144"], count=1, key="10.0.0.1", items=[it])]
    doc = render_report_html(ctx)
    assert 'id="sec-rollups"' in doc
    assert "Findings by vulnerability" in doc and "Findings by host" in doc
    assert "EternalBlue" in doc
    assert "10.0.0.1, 10.0.0.2, 10.0.0.3" in doc          # the collapsed affected-hosts cell
    assert "https://nvd.nist.gov" not in doc.split('id="sec-rollups"')[1].split("</section>")[0], \
        "the deliverable rollup is text-only (print-safe), no external NVD links"
    assert "Findings by Vulnerability" in doc              # nav chip + print TOC entry


# --------------------------------------------------------------------------- context (visible-only)

def test_deliverable_rollup_feeds_the_seam_report_visible_findings_only(app, stub_host, session_factory):
    # A non-empty return so ctx.findings_by_kind is populated; the assertion of interest is the ROWS
    # the seam was FED (the deliverable's report-visible filter), captured by the stub.
    stub_host.group_findings_value = [_bucket(label="x", severity="info", hosts=[], cves=[], count=0,
                                              key="x")]
    with session_factory() as db:
        client = Client(name="Acme")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="Q3", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Internal", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        db.add_all([
            BoardFinding(engagement_id=eng.id, group_id=grp.id, title="VisibleVuln",
                         severity=Severity.high, target_host="10.0.0.1",
                         source_facts={"dedupe_key": "k1", "source": "nuclei"}, include_in_report=True),
            BoardFinding(engagement_id=eng.id, group_id=grp.id, title="ExcludedVuln",
                         severity=Severity.high, target_host="10.0.0.2",
                         source_facts={"dedupe_key": "k2", "source": "nuclei"}, include_in_report=False),
        ])
        db.commit()
        eng_id = eng.id

    stub_host.group_findings_calls.clear()
    with app.app_context():
        with session_factory() as db:
            ctx = build_report_context(db.get(ReportBoard, eng_id))

    # Wiring: the seam is asked for both pivots and its result is threaded onto the context.
    assert [by for _, by in stub_host.group_findings_calls] == ["kind", "host"]
    assert ctx.findings_by_kind == stub_host.group_findings_value
    # Deliverable-specific: the excluded finding is NOT in the rows fed to the seam (unlike the board,
    # which groups ALL findings). This is the client-rollup-never-leaks-excluded guarantee.
    rows, _ = stub_host.group_findings_calls[0]
    labels = {r["label"] for r in rows}
    assert "VisibleVuln" in labels
    assert "ExcludedVuln" not in labels
