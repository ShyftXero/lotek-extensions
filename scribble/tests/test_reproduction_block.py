"""The first-class ``reproduction`` content block (copy-pastable repro steps, omit-when-empty) and its
deterministic auto-fill from a promoted scan finding's PoC facts.

The block is deliberately a ProseMirror ``codeBlock`` so it renders ``<pre><code>`` — copy-pastable —
and it rides the SAME omit-when-empty path as every other block, so a finding with no PoC simply drops
the section from the report.
"""
from __future__ import annotations

from scribble.api_pat import _PATCH_CONTENT_FIELDS, _author_content_json
from scribble.content import schema
from scribble.content.render_html import render_block
from scribble.models import EngagementFinding
from scribble.reporting.render_html import _BLOCK_LABELS, _BLOCK_ORDER
from tests.conftest import FakeFindingDTO

# ── schema.code_block_doc ────────────────────────────────────────────────────────────────────────


def test_code_block_doc_wraps_text_verbatim():
    doc = schema.code_block_doc("line 1\nline 2")
    block = doc["content"][0]
    assert block["type"] == schema.CODE_BLOCK
    assert block["content"][0]["text"] == "line 1\nline 2"


def test_code_block_doc_empty_is_empty_doc():
    assert schema.code_block_doc("") == schema.empty_doc()
    assert schema.code_block_doc("\n\n") == schema.empty_doc()


# ── rendering: copy-pastable + first-class position ──────────────────────────────────────────────


def test_reproduction_renders_as_a_copy_pastable_code_block():
    html = render_block(schema.code_block_doc("curl -H 'x-tn-id: <id>' https://h/api"))
    assert "<pre><code>" in html and "</code></pre>" in html
    # Quotes are preserved (copy-pastable); only the HTML-significant < > are escaped.
    assert "curl -H 'x-tn-id: &lt;id&gt;' https://h/api" in html


def test_reproduction_is_a_first_class_block_after_details():
    assert _BLOCK_LABELS["reproduction"] == "Reproduction"
    assert _BLOCK_ORDER.index("reproduction") > _BLOCK_ORDER.index("details")


# ── auto-fill on promotion (EngagementFinding.from_lotek_finding is the single home) ──────────────


def test_promoted_finding_with_a_curl_fact_gets_a_reproduction_block():
    dto = FakeFindingDTO(id=1, title="Exposed endpoint", facts={"curl": "curl https://h/api"})
    finding = EngagementFinding.from_lotek_finding(dto)
    repro = finding.content_json.get("reproduction")
    assert repro is not None, "a promoted finding carrying a curl PoC must gain a reproduction block"
    assert repro["content"][0]["type"] == schema.CODE_BLOCK
    assert "curl https://h/api" in repro["content"][0]["content"][0]["text"]


def test_promoted_finding_without_a_poc_has_no_reproduction_block():
    dto = FakeFindingDTO(id=2, title="Plain finding", facts={"host": "h", "cwe": "CWE-200"})
    finding = EngagementFinding.from_lotek_finding(dto)
    assert "reproduction" not in finding.content_json  # empty -> absent -> omitted from the report


def test_auto_fill_does_not_suppress_the_details_fallback():
    # A finding with raw evidence AND a PoC keeps BOTH: details (from evidence) and reproduction.
    dto = FakeFindingDTO(id=3, title="Both", evidence="anonymous share found",
                         facts={"curl": "smbclient -N //h/share"})
    finding = EngagementFinding.from_lotek_finding(dto)
    assert "details" in finding.content_json
    assert "reproduction" in finding.content_json


# ── authoring: the plain-text convenience field + PATCH allow-list ───────────────────────────────


def test_authoring_wraps_plain_text_reproduction_as_a_code_block():
    blocks = _author_content_json({"reproduction": "step 1\nstep 2"})
    repro = blocks["reproduction"]
    assert repro["content"][0]["type"] == schema.CODE_BLOCK
    assert repro["content"][0]["content"][0]["text"] == "step 1\nstep 2"


def test_reproduction_is_a_patchable_content_field():
    assert "reproduction" in _PATCH_CONTENT_FIELDS
