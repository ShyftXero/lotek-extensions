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


def test_reproduction_ssti_payload_renders_verbatim_not_evaluated(session_factory):
    """A curl PoC carrying a template-injection payload ({{7*7}}) must render VERBATIM in the
    reproduction block — the report's Jinja resolver must NOT evaluate it to 49, or the copy-pastable
    repro stops reproducing the bug (and the report becomes an SSTI sink)."""
    from scribble.models import Engagement, FindingGroup
    from scribble.promote import promote_one
    from scribble.reporting import build_report_context
    from scribble.reporting.render_html import render_report_html
    from tests.conftest import FakeFindingDTO

    dto = FakeFindingDTO(id=1, title="SSTI", severity="high",
                         facts={"curl": "curl 'https://h/?q={{7*7}}'"})
    with session_factory() as db:
        eng = Engagement(name="Co", company_name="Acme")
        group = FindingGroup(engagement=eng, name="Findings", order_index=0)
        db.add(eng)
        db.flush()
        promote_one(db, engagement=eng, group=group, dto=dto, actor_username="t", order_index=0)
        db.commit()
        eng_id = eng.id
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eng_id)))
    assert "q={{7*7}}" in html, "the SSTI payload must survive verbatim in the reproduction block"
    assert "q=49" not in html, "the reproduction block must not be Jinja-evaluated"


def test_patch_reproduction_clears_on_empty_and_wraps_a_string():
    """PATCH parity with description/remediation: an empty string CLEARS the block (was a silent no-op),
    a string wraps as a code block. (Clear/valid paths take no app context — no jsonify.)"""
    from scribble.api_pat import _patch_content_blocks
    from scribble.content import schema

    blocks, err = _patch_content_blocks({"reproduction": ""})
    assert err is None and blocks.get("reproduction") == schema.empty_doc(), "empty must clear the block"
    blocks, err = _patch_content_blocks({"reproduction": "curl x"})
    assert err is None and blocks["reproduction"]["content"][0]["type"] == schema.CODE_BLOCK


def test_patch_reproduction_rejects_a_non_string(app):
    """A non-string reproduction is a 400 (not silently dropped) — the strict-type guard now covers it."""
    from scribble.api_pat import _patch_content_blocks

    with app.app_context():
        _blocks, err = _patch_content_blocks({"reproduction": 123})
    assert err is not None, "a non-string reproduction must be rejected, not silently ignored"
