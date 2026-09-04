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
    # the index metadata cells are present as headers but the row cells are the empty em-dash.
    assert "<th>CWE</th>" in html and "<th>CVE</th>" in html


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
