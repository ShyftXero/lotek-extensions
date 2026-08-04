"""WS12: the FACTION seed import produces real ProseMirror structure, not `doc_from_text`-wrapped
HTML/markdown-ish blobs.

Regression context: the Sprint 0 importer (`fraction/seed/loader.py`) wrapped the *entire* FACTION
`Description` field -- which embeds real HTML (`<p>`, `<ul>`, `<li>`, `<code>`, `<span>`) *and*
`# Description` / `# Impact` / `# Replication Steps` section markers -- as plain text via
`schema.doc_from_text`. Reports rendered literal `# Description <p>...</p>` instead of paragraphs and
lists. These tests assert the real end-state per RAILS.md §4: actual node structure (paragraph/
bulletList/codeBlock nodes, not one text blob) and real rendered HTML (`<p>`/`<ul>` tags, not escaped
text) -- not just "some text is present somewhere".
"""

from __future__ import annotations

import re

from fraction.content import render_html, schema
from fraction.models import VulnerabilityTemplate
from fraction.seed import faction_parse

_KERBEROAST = "At Least One Member of an Admin Group Is Vulnerable to the Kerberoast Attack"
_NO_EDR_RECOMMENDATION = "Using EDR - Microsoft Defender for Endpoint"  # empty Recommendation in source
_NO_PASSWORD_POLICY = "No Password Policy"  # source has a `{{.pass_pol}}` foreign token


def _node_types(doc: dict) -> set[str]:
    return {n.get("type") for n in schema.iter_nodes(doc)}


def _template(session_factory, name: str) -> VulnerabilityTemplate:
    with session_factory() as db:
        tmpl = db.query(VulnerabilityTemplate).filter_by(name=name).one()
        # Detach the bits the tests need past the session's lifetime.
        return tmpl


# ---------------------------------------------------------------------------
# Real structure, not a text blob (the core regression)
# ---------------------------------------------------------------------------


def test_kerberoast_description_is_real_structure(session_factory):
    tmpl = _template(session_factory, _KERBEROAST)
    doc = tmpl.content_json["description"]

    # Real block structure: at least a paragraph AND a bulletList/listItem, not a single wrapped blob.
    types = _node_types(doc)
    assert {schema.PARAGRAPH, schema.BULLET_LIST, schema.LIST_ITEM} <= types
    assert len(doc["content"]) > 1, "expected multiple top-level block nodes, not one giant paragraph"

    text = schema.plain_text(doc)
    assert "<p>" not in text
    assert "<ul>" not in text and "<li>" not in text
    assert "# Description" not in text
    assert "# Impact" not in text
    assert "# Replication Steps" not in text


def test_kerberoast_description_renders_real_html(session_factory):
    tmpl = _template(session_factory, _KERBEROAST)
    doc = tmpl.content_json["description"]
    html = render_html.render_block(doc)

    assert "<p>" in html
    assert "<ul>" in html and "<li>" in html
    # Not double-escaped (i.e. the parser produced real nodes, not a text node containing "<p>").
    assert "&lt;p&gt;" not in html
    assert "&lt;ul&gt;" not in html
    # Cached content_html matches a fresh render.
    assert tmpl.content_html["description"] == html


def test_kerberoast_details_and_remediation_populated(session_factory):
    tmpl = _template(session_factory, _KERBEROAST)
    details = tmpl.content_json["details"]
    remediation = tmpl.content_json["remediation"]

    # Replication Steps in the source includes two PowerShell one-liners -> real codeBlock nodes.
    assert schema.CODE_BLOCK in _node_types(details)
    assert "get-aduser" in schema.plain_text(details)
    details_html = render_html.render_block(details)
    assert "<pre>" in details_html and "<code>" in details_html

    # Recommendation in the source is a bullet list -> real bulletList/listItem nodes.
    assert schema.BULLET_LIST in _node_types(remediation)
    rem_html = render_html.render_block(remediation)
    assert "<ul>" in rem_html and "<li>" in rem_html
    assert tmpl.content_html["details"] == details_html
    assert tmpl.content_html["remediation"] == rem_html


def test_impact_folded_under_bold_lead_paragraph(session_factory):
    tmpl = _template(session_factory, _KERBEROAST)
    content = tmpl.content_json["description"]["content"]

    # Index into the FULL top-level content list (not a paragraph-only projection) by matching the
    # node whose text is exactly "Impact" -- so this can't coincidentally land on some other
    # bold-prefixed paragraph (e.g. the "PingCastle Note:" one).
    impact_idx = next(
        i for i, node in enumerate(content) if schema.plain_text(node).strip() == "Impact"
    )
    lead = content[impact_idx]
    assert lead["type"] == schema.PARAGRAPH
    lead_text_node = lead["content"][0]
    # The lead-in MUST actually be bold: if bold_lead_paragraph regressed to a plain paragraph this
    # assertion fails (verified fail-before/pass-after by reverting bold_lead_paragraph -> paragraph).
    assert {"type": "bold"} in lead_text_node.get("marks", [])
    # The folded-in Impact section content is the immediately following top-level node.
    assert impact_idx + 1 < len(content), "expected impact body to follow the bold lead paragraph"
    assert "Complete enviornment compromise" in schema.plain_text(content[impact_idx + 1])


def test_empty_recommendation_yields_empty_doc_not_stray_header(session_factory):
    tmpl = _template(session_factory, _NO_EDR_RECOMMENDATION)
    remediation = tmpl.content_json["remediation"]
    assert remediation == schema.empty_doc()
    assert render_html.render_block(remediation) == ""
    assert tmpl.content_html["remediation"] == ""


# ---------------------------------------------------------------------------
# Whole-library invariants
# ---------------------------------------------------------------------------


def test_seeded_template_count(session_factory):
    # 44 FACTION default library + 19 lotek AD/network entries (lotek_vulnerabilities.json).
    with session_factory() as db:
        assert db.query(VulnerabilityTemplate).count() == 63


def test_company_name_present_no_raw_client_or_foreign_tokens_remain(session_factory):
    with session_factory() as db:
        templates = db.query(VulnerabilityTemplate).all()
        all_blocks = [
            tmpl.content_json.get(block, schema.empty_doc())
            for tmpl in templates
            for block in schema.DEFAULT_BLOCKS
        ]
    blob = " ".join(schema.plain_text(doc) for doc in all_blocks)

    assert "{{COMPANY_NAME}}" in blob
    # No raw FACTION-internal dot-tokens survive (they must be normalized to bracket literals).
    assert "{{." not in blob
    # No bare "CLIENT" word remains unconverted (word-boundary check, same rule the importer applies).
    assert re.search(r"\bCLIENT\b", blob) is None
    # And no lowercase {{client}} token remains either.
    assert "{{client}}" not in blob.lower()
    # The FACTION testing firm's name must never ship in seeded content (it's not the client).
    assert "NemesisGroup" not in blob


def test_foreign_dot_token_normalized_to_bracket_literal(session_factory):
    tmpl = _template(session_factory, _NO_PASSWORD_POLICY)
    docs = (tmpl.content_json.get(b, schema.empty_doc()) for b in schema.DEFAULT_BLOCKS)
    blob = " ".join(schema.plain_text(doc) for doc in docs)
    assert "[pass_pol]" in blob
    assert "{{.pass_pol}}" not in blob


# ---------------------------------------------------------------------------
# faction_parse unit tests: defensive HTML handling (no DB needed)
# ---------------------------------------------------------------------------


def test_split_sections_handles_missing_markers():
    sections = faction_parse.split_description_sections("just plain text, no section markers at all")
    assert sections["description"] == "just plain text, no section markers at all"
    assert sections["impact"] == ""
    assert sections["replication"] == ""


def test_split_sections_empty_input():
    sections = faction_parse.split_description_sections("")
    assert sections == {"description": "", "impact": "", "replication": ""}
    sections_none = faction_parse.split_description_sections(None)
    assert sections_none == {"description": "", "impact": "", "replication": ""}


def test_build_template_blocks_empty_record_yields_all_empty_docs():
    blocks = faction_parse.build_template_blocks("", "")
    assert blocks["description"] == schema.empty_doc()
    assert blocks["details"] == schema.empty_doc()
    assert blocks["remediation"] == schema.empty_doc()


def test_split_sections_ignores_hash_marker_mid_prose():
    # A literal "# Impact" appearing inside a sentence (not at line start) must NOT split the record.
    desc = "# Description\n<p>We rate the # Impact of this below.</p>\n\n# Impact\n<p>High.</p>"
    sections = faction_parse.split_description_sections(desc)
    # The real (line-start) "# Impact" marker splits; the in-prose one stays in the description body.
    assert "We rate the # Impact of this below." in sections["description"]
    assert "<p>High.</p>" in sections["impact"]
    assert sections["replication"] == ""


def test_nested_list_only_item_gets_leading_paragraph():
    # A <li> whose only child is a nested list must still start with a paragraph (TipTap's listItem
    # content model is `paragraph block*`), or content can be dropped on an editor/Yjs round-trip.
    doc = faction_parse.html_to_doc("<ul><li><ul><li>deep</li></ul></li></ul>")
    outer_list = doc["content"][0]
    assert outer_list["type"] == schema.BULLET_LIST
    outer_item = outer_list["content"][0]
    assert outer_item["type"] == schema.LIST_ITEM
    first_child = outer_item["content"][0]
    assert first_child["type"] == schema.PARAGRAPH  # the prepended lead paragraph
    # The nested list content survives.
    assert outer_item["content"][1]["type"] == schema.BULLET_LIST
    assert "deep" in schema.plain_text(doc)
    # And it renders without crashing.
    assert "<ul>" in render_html.render_block(doc)


def test_normal_list_item_not_given_spurious_paragraph():
    # A plain text <li> already starts with a paragraph -> no extra empty one prepended.
    doc = faction_parse.html_to_doc("<ul><li>plain item</li></ul>")
    item = doc["content"][0]["content"][0]
    paragraphs = [b for b in item["content"] if b["type"] == schema.PARAGRAPH]
    assert len(paragraphs) == 1
    assert schema.plain_text(item) == "plain item"


def test_bare_top_level_inline_coalesces_into_one_paragraph():
    # No wrapping <p>: three inline fragments must become ONE paragraph, not three.
    doc = faction_parse.html_to_doc("Some text <strong>bold</strong> more text")
    paragraphs = [n for n in doc["content"] if n["type"] == schema.PARAGRAPH]
    assert len(doc["content"]) == 1
    assert len(paragraphs) == 1
    assert schema.plain_text(doc) == "Some text bold more text"
    bold_marks = {
        mark.get("type")
        for n in schema.iter_nodes(paragraphs[0])
        if n.get("type") == schema.TEXT
        for mark in n.get("marks", [])
    }
    assert "bold" in bold_marks


def test_blank_gap_between_blocks_makes_no_empty_paragraph():
    # The "\n\n" whitespace between two <p> elements must not become a spurious empty paragraph.
    doc = faction_parse.html_to_doc("<p>first</p>\n\n<p>second</p>")
    assert [n["type"] for n in doc["content"]] == [schema.PARAGRAPH, schema.PARAGRAPH]
    assert schema.plain_text(doc) == "firstsecond"


def test_firm_name_neutralized_not_treated_as_company():
    out = faction_parse.normalize_tokens("During the assessment, NemesisGroup found an issue.")
    assert "NemesisGroup" not in out
    assert "the assessment team" in out
    assert "{{COMPANY_NAME}}" not in out  # the assessor is not the client


def test_html_to_doc_unclosed_tag_never_crashes():
    doc = faction_parse.html_to_doc("<p>Unclosed <strong>bold text")
    assert schema.plain_text(doc) == "Unclosed bold text"
    html = render_html.render_block(doc)
    assert "<strong>" in html and "bold text" in html


def test_html_to_doc_unknown_tag_degrades_to_text():
    doc = faction_parse.html_to_doc('<p>Hello <marquee class="x">world</marquee>!</p>')
    assert schema.plain_text(doc) == "Hello world!"
    assert "marquee" not in render_html.render_block(doc)


def test_html_to_doc_mismatched_close_tags_never_crash():
    # Malformed source: </p> closes both <strong> and <p>; the trailing </strong> has no open match.
    doc = faction_parse.html_to_doc("<p>Open <strong>bold</p> stray close</strong>")
    text = schema.plain_text(doc)
    assert "Open" in text and "bold" in text and "stray close" in text


def test_html_to_doc_empty_and_none_yield_empty_doc():
    assert faction_parse.html_to_doc("") == schema.empty_doc()
    assert faction_parse.html_to_doc(None) == schema.empty_doc()
    assert faction_parse.html_to_doc("   \n  ") == schema.empty_doc()


def test_normalize_tokens_client_variants_and_foreign_tokens():
    assert faction_parse.normalize_tokens("{{client}}") == "{{COMPANY_NAME}}"
    assert faction_parse.normalize_tokens("{{ Client }}") == "{{COMPANY_NAME}}"
    assert "{{COMPANY_NAME}}" in faction_parse.normalize_tokens("CLIENT provided access")
    assert faction_parse.normalize_tokens("{{.pass_pol}}") == "[pass_pol]"
    assert faction_parse.normalize_tokens("{{.pwd_reuse_0}}") == "[pwd_reuse_0]"
    # A real, valid token must survive untouched.
    assert faction_parse.normalize_tokens("{{COMPANY_NAME}}") == "{{COMPANY_NAME}}"


def test_html_to_doc_preserves_bold_italic_link_and_code_marks():
    doc = faction_parse.html_to_doc(
        '<p><span class="bold">Bold</span> <span class="italic">Italic</span> '
        '<a href="https://example.com">link</a> <code>inline_code</code></p>'
    )
    marks = {
        mark.get("type")
        for node in schema.iter_nodes(doc)
        if node.get("type") == schema.TEXT
        for mark in node.get("marks", [])
    }
    assert {"bold", "italic", "link", "code"} <= marks
    html = render_html.render_block(doc)
    assert "<strong>Bold</strong>" in html
    assert "<em>Italic</em>" in html
    assert '<a href="https://example.com"' in html and ">link</a>" in html
    assert "<code>inline_code</code>" in html
