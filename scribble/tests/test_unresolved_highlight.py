"""LOT-63: unpopulated report tags are highlighted yellow at render time.

Two cases must both surface (loud, not silent): an *unknown* tag (no such variable), and a *known but
empty* tag (the variable exists but resolved to nothing). Detection was already solved by the lint path;
this pins the render-time presentation change across all three layers: the resolver stamps an
``unresolvedVar`` mark, the HTML renderer turns it into a ``.unresolved-var`` span, and the DOCX
converter applies a yellow run-shading fill so the highlight survives into the PDF a client receives.
"""

from __future__ import annotations

import docx
import pytest
from docxtpl import DocxTemplate

from scribble.content import schema
from scribble.content.render_docx import html_to_richtext
from scribble.content.render_html import render_block
from scribble.templating.resolver import resolve_doc

_CTX = {"COMPANY_NAME": "Acme Corp", "TARGET_HOST": "", "SEVERITY": None}


def _para(*nodes) -> dict:
    return {"type": schema.DOC, "content": [{"type": schema.PARAGRAPH, "content": list(nodes)}]}


def _first_paragraph_children(doc: dict) -> list[dict]:
    return doc["content"][0]["content"]


# --------------------------------------------------------------------------- resolve_doc marking


def test_variable_node_populated_is_not_highlighted():
    doc = _para({"type": schema.VARIABLE, "attrs": {"key": "COMPANY_NAME"}})
    out = _first_paragraph_children(resolve_doc(doc, _CTX))
    assert out == [{"type": schema.TEXT, "text": "Acme Corp"}]


def test_variable_node_unknown_is_highlighted():
    doc = _para({"type": schema.VARIABLE, "attrs": {"key": "NOPE"}})
    (node,) = _first_paragraph_children(resolve_doc(doc, _CTX))
    assert node["text"] == "{{NOPE}}"
    assert {"type": schema.MARK_UNRESOLVED} in node["marks"]


def test_variable_node_known_but_empty_is_highlighted():
    doc = _para({"type": schema.VARIABLE, "attrs": {"key": "TARGET_HOST"}})
    (node,) = _first_paragraph_children(resolve_doc(doc, _CTX))
    assert node["text"] == "{{TARGET_HOST}}"
    assert {"type": schema.MARK_UNRESOLVED} in node["marks"]


def test_inline_token_split_marks_only_the_unresolved_part():
    # One text node mixing a resolved builtin, an empty builtin, and an unknown tag.
    doc = _para({"type": schema.TEXT, "text": "For {{COMPANY_NAME}} on {{TARGET_HOST}} see {{NOPE}}."})
    out = _first_paragraph_children(resolve_doc(doc, _CTX))

    def _marked(node: dict) -> bool:
        return any(m.get("type") == schema.MARK_UNRESOLVED for m in node.get("marks", []))

    rendered = [(n["text"], _marked(n)) for n in out]
    assert rendered == [
        ("For Acme Corp on ", False),
        ("{{TARGET_HOST}}", True),
        (" see ", False),
        ("{{NOPE}}", True),
        (".", False),
    ]


def test_inline_split_preserves_existing_marks():
    doc = _para({"type": schema.TEXT, "text": "{{NOPE}}", "marks": [{"type": "bold"}]})
    (node,) = _first_paragraph_children(resolve_doc(doc, _CTX))
    types = {m["type"] for m in node["marks"]}
    assert types == {"bold", schema.MARK_UNRESOLVED}


def test_fully_resolved_text_stays_one_unmarked_node():
    doc = _para({"type": schema.TEXT, "text": "Hi {{COMPANY_NAME}}"})
    out = _first_paragraph_children(resolve_doc(doc, _CTX))
    assert out == [{"type": schema.TEXT, "text": "Hi Acme Corp"}]


# --------------------------------------------------------------------------- HTML rendering


def test_html_wraps_unresolved_mark_in_highlight_span():
    resolved = resolve_doc(_para({"type": schema.VARIABLE, "attrs": {"key": "NOPE"}}), _CTX)
    html = render_block(resolved)
    assert '<span class="unresolved-var">{{NOPE}}</span>' in html


def test_html_variable_branch_highlights_unpopulated_directly():
    # A variable node rendered WITHOUT pre-resolution (resolve_var returns empty) still highlights.
    doc = _para({"type": schema.VARIABLE, "attrs": {"key": "COMPANY_NAME"}})
    html = render_block(doc, resolve_var=lambda _k: "")
    assert '<span class="unresolved-var">{{COMPANY_NAME}}</span>' in html


def test_html_resolved_value_is_not_highlighted():
    resolved = resolve_doc(_para({"type": schema.VARIABLE, "attrs": {"key": "COMPANY_NAME"}}), _CTX)
    html = render_block(resolved)
    assert "Acme Corp" in html
    assert "unresolved-var" not in html


# --------------------------------------------------------------------------- DOCX rendering


@pytest.fixture
def tpl(tmp_path):
    doc = docx.Document()
    doc.add_paragraph("{{r body}}")
    path = tmp_path / "mini.docx"
    doc.save(str(path))
    t = DocxTemplate(str(path))
    t.init_docx()
    t.current_rendering_part = t.docx.part
    return t


def test_docx_highlight_span_becomes_yellow_shading(tpl):
    html = '<p>Client: <span class="unresolved-var">{{COMPANY_NAME}}</span> done</p>'
    rt = html_to_richtext(html, tpl=tpl)
    assert 'w:fill="ffff00"' in rt.xml  # the highlighted token carries a yellow shading fill
    assert "{{COMPANY_NAME}}" in rt.xml
    # Surrounding prose is a plain run with no shading.
    assert "Client:" in rt.xml


def test_docx_plain_span_is_not_shaded(tpl):
    rt = html_to_richtext('<p>Plain <span class="other">text</span> here</p>', tpl=tpl)
    assert "w:shd" not in rt.xml
