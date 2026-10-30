"""The optional comprehensive Host Appendix (ext#254).

An OPT-IN report appendix — included by putting the ``host_appendix`` block in a Layout (the
"checkbox"), off by default — that enumerates every host identified in a finding, host-sorted, as a
reference list at the end of the report. These pin: it reuses the SAME by-host buckets the in-body
rollup renders (``ctx.findings_by_host``), it appears (with its TOC entry) only when opted in AND there
is host data, it sorts hosts naturally (``.2`` before ``.10``), and it escapes engagement-controlled
text.

Like the rollup tests, the by-host seam returns ``[]`` offline (it lives in mounted lotek core), so the
buckets the in-request seam would supply are injected onto the built context directly.
"""

from __future__ import annotations

import io
import re
from types import SimpleNamespace

import docx
from docx.oxml.ns import qn

from scribble.enums import Severity
from scribble.models import BoardFinding, FindingGroup, ReportBoard
from scribble.reporting import build_report_context
from scribble.reporting.layouts import ReportLayout
from scribble.reporting.render_docx import render_report_docx
from scribble.reporting.render_html import (
    _AssetResolver,
    _render_document,
    _render_host_appendix,
    _toc_entries,
)

_WITH_HOSTS = ReportLayout("with-hosts", "With hosts", ("cover", "toc", "findings", "host_appendix"))
_WITHOUT_HOSTS = ReportLayout("no-hosts", "No hosts", ("cover", "toc", "findings"))


def _bucket(key: str, severity: str, count: int, labels: list[str], cves: list[str]) -> SimpleNamespace:
    return SimpleNamespace(key=key, severity=severity, count=count, cves=cves,
                           items=[SimpleNamespace(label=lbl) for lbl in labels])


def _ctx_with_hosts(session_factory, buckets):
    with session_factory() as db:
        eng = ReportBoard(name="Host Co Assessment")
        grp = FindingGroup(engagement=eng, name="Findings", order_index=0)
        BoardFinding(engagement=eng, group=grp, title="SMB signing not required",
                     severity=Severity.high, order_index=0, target_host="192.0.2.10")
        db.add_all([eng, grp])
        db.commit()
        eid = eng.id
    with session_factory() as db:
        ctx = build_report_context(db.get(ReportBoard, eid))
    ctx.findings_by_host = buckets  # inject what the in-request seam would supply
    return ctx


def _toc_targets(html: str) -> list[str]:
    m = re.search(r'<nav class="toc".*?</nav>', html, re.S)
    return re.findall(r'href="#([^"]+)"', m.group(0)) if m else []


def test_appendix_and_toc_entry_appear_only_when_opted_in(session_factory):
    ctx = _ctx_with_hosts(session_factory, [
        _bucket("192.0.2.2", "low", 1, ["Anonymous FTP enabled"], []),
        _bucket("192.0.2.10", "high", 1, ["SMB signing not required"], ["CVE-2017-0144"]),
    ])
    with_html = _render_document(ctx, _AssetResolver("none", None), layout=_WITH_HOSTS)
    assert 'id="sec-host_appendix"' in with_html
    assert "Host Appendix" in with_html
    assert "sec-host_appendix" in _toc_targets(with_html)
    assert "192.0.2.2" in with_html and "192.0.2.10" in with_html
    # opt-out: no block -> no section, no contents entry (byte-identical path to pre-block)
    without_html = _render_document(ctx, _AssetResolver("none", None), layout=_WITHOUT_HOSTS)
    assert 'id="sec-host_appendix"' not in without_html
    assert "sec-host_appendix" not in _toc_targets(without_html)


def test_hosts_sorted_naturally_not_lexically(session_factory):
    """``192.0.2.2`` sorts before ``192.0.2.10`` — an inventory reads in host order, not string order —
    even when the seam hands the buckets back severity-first (``.10`` is the worse host)."""
    ctx = _ctx_with_hosts(session_factory, [
        _bucket("192.0.2.10", "high", 1, ["SMB signing not required"], []),
        _bucket("192.0.2.2", "low", 1, ["Anonymous FTP enabled"], []),
    ])
    html = _render_host_appendix(ctx)
    assert html.index("192.0.2.2") < html.index("192.0.2.10")


def test_empty_host_data_omits_the_appendix_and_its_toc_entry():
    """No host-bucketed findings (e.g. off-mount) -> renders nothing and registers no TOC entry, so a
    Layout that opts the block in for an engagement with no host data stays clean."""
    empty = SimpleNamespace(findings_by_host=[])
    assert _render_host_appendix(empty) == ""
    assert _toc_entries(empty, ("host_appendix",)) == []


def test_docx_appendix_honors_the_opt_in(session_factory):
    """DOCX parity: the Host Appendix renders in the .docx only when ``host_appendix`` is in the enabled
    section order (the same opt-in the HTML preview reads), with the same host rows."""
    def _text(payload: bytes) -> str:
        d = docx.Document(io.BytesIO(payload))
        return "".join(t.text or "" for t in d.element.body.iter(qn("w:t")))

    ctx = _ctx_with_hosts(session_factory, [
        _bucket("192.0.2.2", "low", 1, ["Anonymous FTP enabled"], []),
        _bucket("192.0.2.10", "high", 1, ["SMB signing not required"], ["CVE-2017-0144"]),
    ])
    # opted out (default order omits it) -> no appendix in the .docx
    off = _text(render_report_docx(ctx))
    assert "Host Appendix" not in off
    # opted in -> appendix with its host rows
    ctx.section_order = (*ctx.section_order, "host_appendix")
    on = _text(render_report_docx(ctx))
    assert "Host Appendix" in on
    assert "192.0.2.2" in on and "192.0.2.10" in on


def test_host_labels_and_vulns_are_escaped(session_factory):
    """Host labels and vuln titles ride on engagement-controlled finding data, so they must be escaped."""
    ctx = _ctx_with_hosts(session_factory, [
        _bucket("<script>alert(1)</script>", "medium", 1, ["<b>Bad cert</b>"], []),
    ])
    html = _render_document(ctx, _AssetResolver("none", None), layout=_WITH_HOSTS)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>Bad cert</b>" not in html
