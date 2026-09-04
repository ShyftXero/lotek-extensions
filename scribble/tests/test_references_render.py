"""Rendering the #624 references block + #625 metadata chips/index columns (map #616).

Builds engagements with hand-set typed columns and asserts the HTML renderer + the DOCX body helpers:
references render as an OMIT-WHEN-EMPTY labeled-link block (non-suppressed only), metadata renders as
finding-header chips + CWE/CVE index columns, and an UNENRICHED finding adds no such markup at all.
"""

from __future__ import annotations

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import Client, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import _metadata_line_html, _references_html
from scribble.reporting.render_html import render_report_html


def _snapshot() -> dict:
    return {"as_of": "2026-09-04", "source": "exploiteer",
            "cves": {"CVE-2021-44228": {"kev": True, "epss": 0.97}}}


def _render(session_factory, **finding_kwargs) -> str:
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = Engagement(name="E", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Web", order_index=0)
        EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high, order_index=0,
            content_json={"description": schema.doc_from_text("A parameter reflects input.")},
            **finding_kwargs,
        )
        db.add(eng)
        db.flush()
        return render_report_html(build_report_context(eng))


def test_enriched_finding_renders_refs_block_and_chips(session_factory):
    html = _render(
        session_factory,
        references=[
            {"label": "OWASP XSS", "url": "https://owasp.org/xss", "source": "template",
             "suppressed": False},
            {"label": "CWE-79", "url": "", "source": "scan", "suppressed": False},
            {"label": "noisy", "url": "https://noisy/1", "source": "scan", "suppressed": True},
        ],
        cve_ids=["CVE-2021-44228"], cwe_ids=["CWE-79"], owasp_categories=["A03:2021"],
        threat_intel=_snapshot(),
    )
    # References block present, non-suppressed rendered as a labeled link + a plain-text (no-url) label.
    assert "References" in html
    assert '<a href="https://owasp.org/xss"' in html
    assert ">OWASP XSS</a>" in html
    assert "noisy" not in html                       # suppressed ref omitted
    # metadata chips beside CVSS.
    assert 'class="chip cwe"' in html and "CWE-79" in html
    assert 'class="chip cve"' in html and "CVE-2021-44228" in html
    assert 'class="chip owasp"' in html and "A03:2021" in html
    assert 'class="chip kev"' in html and "KEV as of 2026-09-04" in html
    assert 'class="chip epss"' in html and "EPSS 0.97" in html
    # index columns.
    assert "<th>CWE</th>" in html and "<th>CVE</th>" in html
    assert 'class="ix-kev"' in html                  # KEV flag in the index row


def test_all_suppressed_references_omit_the_block(session_factory):
    html = _render(
        session_factory,
        references=[{"label": "x", "url": "https://x", "source": "scan", "suppressed": True}],
    )
    # omit-not-show: a finding with no NON-suppressed refs has no References block at all.
    assert '<div class="block references">' not in html


def test_unenriched_finding_has_no_metadata_markup(session_factory):
    html = _render(session_factory)  # no references, no cve/cwe/owasp, no threat_intel
    assert '<div class="block references">' not in html
    assert 'class="chip cwe"' not in html
    assert 'class="chip cve"' not in html
    assert 'class="chip kev"' not in html
    # an unenriched report's index is BYTE-IDENTICAL to before #625: the CWE/CVE columns appear ONLY when
    # some finding carries that data, so here they are absent entirely (not empty-celled).
    assert "<th>CWE</th>" not in html and "<th>CVE</th>" not in html


# ── DOCX body helpers (pure — no docx template boot needed) ──────────────────────────────────────────

class _F:
    """A FindingCtx-shaped stand-in for the DOCX helpers (they read only these attributes)."""
    def __init__(self, **kw):
        self.references = kw.get("references", [])
        self.cve_ids = kw.get("cve_ids", [])
        self.cwe_ids = kw.get("cwe_ids", [])
        self.owasp_categories = kw.get("owasp_categories", [])
        self.threat_intel = kw.get("threat_intel")


def test_docx_metadata_line_omit_when_empty():
    assert _metadata_line_html(_F()) == ""
    line = _metadata_line_html(_F(cwe_ids=["CWE-79"], cve_ids=["CVE-2021-44228"],
                                  owasp_categories=["A03:2021"],
                                  threat_intel={"kev": True, "epss": 0.97, "as_of": "2026-09-04"}))
    assert "Classification:" in line
    assert "CWE-79" in line and "CVE-2021-44228" in line and "A03:2021" in line
    assert "KEV (as of 2026-09-04)" in line and "EPSS 0.97 (as of 2026-09-04)" in line


def test_docx_references_html_omit_when_empty_and_links():
    assert _references_html(_F()) == ""
    # already-filtered (non-suppressed) refs on the ctx; a url -> <a href>, a bare label -> text.
    refs = [{"label": "OWASP", "url": "https://owasp.org/xss"}, {"label": "CWE-79", "url": ""}]
    out = _references_html(_F(references=refs))
    assert "<h4>References</h4>" in out
    assert '<a href="https://owasp.org/xss">OWASP</a>' in out
    assert "<li>CWE-79</li>" in out


# ── #624 upgrade safety: legacy prose ``references`` block must not silently disappear ────────────────

def _render_with_content(session_factory, *, content_json, **finding_kwargs) -> str:
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = Engagement(name="E", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Web", order_index=0)
        EngagementFinding(
            engagement=eng, group=grp, title="Legacy Finding", severity=Severity.high, order_index=0,
            content_json=content_json, **finding_kwargs,
        )
        db.add(eng)
        db.flush()
        return render_report_html(build_report_context(eng))


def test_legacy_references_prose_block_renders_when_column_empty(session_factory):
    """A finding authored BEFORE #624 kept its references in a ``content_json["references"]`` prose block
    and has an EMPTY structured column (no migration backfills it). That block must still render — the
    #624 suppression is gated on the structured column being non-empty, so an upgrade never silently
    drops an existing finding's references from the report."""
    html = _render_with_content(
        session_factory,
        content_json={
            "description": schema.doc_from_text("desc"),
            "references": schema.doc_from_text("https://legacy.example/advisory"),
        },
        # ``references`` column left at its default ([]/NULL) — pre-#624 stored data.
    )
    assert "https://legacy.example/advisory" in html          # legacy refs NOT silently dropped
    assert '<div class="block references">' not in html       # structured block absent (empty column)


def test_structured_references_suppress_legacy_prose_block(session_factory):
    """When a finding HAS structured references, the legacy prose block is suppressed so the two can't
    double-render — the structured column is the one home (#624)."""
    html = _render_with_content(
        session_factory,
        content_json={
            "description": schema.doc_from_text("desc"),
            "references": schema.doc_from_text("https://legacy.example/advisory"),
        },
        references=[{"label": "Structured", "url": "https://structured.example/ref",
                     "source": "author", "suppressed": False}],
    )
    assert '<div class="block references">' in html           # structured block renders
    assert "https://structured.example/ref" in html
    assert "https://legacy.example/advisory" not in html      # legacy prose block suppressed (no double)
