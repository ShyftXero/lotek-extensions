"""Sanitizer for untrusted ProseMirror JSON (write-scoped PAT -> stored content -> HTML report).

These are stored-XSS guards: a write-scoped PAT will POST arbitrary ``content_json`` that Scribble stores
and later renders into an HTML report a human opens in a browser. ``sanitize_prosemirror`` /
``sanitize_content_json`` are the write-time gate that must strip anything outside a small allowlist.

Written red-then-green: each malicious-payload test asserts the dangerous node/mark is *gone* from the
output (it fails against a pass-through that stores the payload verbatim) while the legitimate content in
the same document survives.
"""

from __future__ import annotations

from typing import Any

from scribble.content import schema
from scribble.prosemirror_sanitize import (
    ALLOWED_MARKS,
    ALLOWED_NODE_TYPES,
    sanitize_content_json,
    sanitize_prosemirror,
)


def _node_types(doc: dict[str, Any]) -> set[str]:
    """Every node type appearing anywhere in the document."""
    return {n.get("type") for n in schema.iter_nodes(doc)}


def _mark_types(doc: dict[str, Any]) -> set[str]:
    """Every mark type on any text node in the document."""
    out: set[str] = set()
    for n in schema.iter_nodes(doc):
        for mark in n.get("marks", []) or []:
            out.add(mark.get("type"))
    return out


def _find_text(doc: dict[str, Any], text: str) -> dict[str, Any] | None:
    for n in schema.iter_nodes(doc):
        if n.get("type") == schema.TEXT and n.get("text") == text:
            return n
    return None


def _doc_with_link(text: str, href: str) -> dict[str, Any]:
    """A one-paragraph doc whose single text node carries a link mark."""
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": text, "marks": [{"type": "link", "attrs": {"href": href}}]}
                ],
            }
        ],
    }


# --- allowlist sanity -------------------------------------------------------------------------------


def test_allowlists_are_exactly_the_documented_sets():
    assert ALLOWED_NODE_TYPES == {
        "doc",
        "paragraph",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "codeBlock",
        "text",
        "hardBreak",
    }
    assert ALLOWED_MARKS == {"bold", "italic", "code", "link"}


# --- the core red-then-green case: raw-HTML node stripped, legit paragraph+bold survives -------------


def test_raw_html_node_stripped_but_paragraph_and_bold_survive():
    payload = {
        "type": "doc",
        "content": [
            # An attacker-supplied raw-HTML node carrying a <script> — NOT an allowlisted type.
            {"type": "html", "html": "<script>alert(document.cookie)</script>"},
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Safe ", "marks": []},
                    {"type": "text", "text": "bold word", "marks": [{"type": "bold"}]},
                ],
            },
        ],
    }

    clean = sanitize_prosemirror(payload)

    # The malicious node is gone entirely (this is the assertion that fails on a pass-through).
    assert "html" not in _node_types(clean)
    assert "script" not in _node_types(clean)
    assert all("script" not in str(n.get(k, "")) for n in schema.iter_nodes(clean) for k in ("text", "html"))

    # The legitimate paragraph and its bold mark survive.
    assert _node_types(clean) <= ALLOWED_NODE_TYPES
    bold = _find_text(clean, "bold word")
    assert bold is not None
    assert bold.get("marks") == [{"type": "bold"}]
    assert _find_text(clean, "Safe ") is not None


def test_script_node_type_is_dropped():
    payload = {
        "type": "doc",
        "content": [
            {"type": "script", "content": [{"type": "text", "text": "evil()"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "keep me"}]},
        ],
    }
    clean = sanitize_prosemirror(payload)
    assert "script" not in _node_types(clean)
    # Children of a dropped node are NOT hoisted — the "evil()" text goes with its parent.
    assert _find_text(clean, "evil()") is None
    assert _find_text(clean, "keep me") is not None


# --- link marks: javascript: dropped, http/https kept ------------------------------------------------


def test_javascript_link_mark_dropped_text_kept():
    payload = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "click me",
                        "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}],
                    }
                ],
            }
        ],
    }
    clean = sanitize_prosemirror(payload)
    # No link mark survives; the text itself is preserved (unlinked).
    assert "link" not in _mark_types(clean)
    node = _find_text(clean, "click me")
    assert node is not None
    assert "javascript" not in str(node.get("marks", []))


def test_non_http_scheme_links_are_all_dropped():
    for bad_href in (
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "  javascript:alert(1)",  # leading whitespace must not evade the allowlist
        "JAVASCRIPT:alert(1)",
        "/relative/path",
        "mailto:a@b.c",
        "ftp://host/x",
    ):
        clean = sanitize_prosemirror(_doc_with_link("x", bad_href))
        assert "link" not in _mark_types(clean), bad_href


def test_http_and_https_link_marks_survive():
    for good_href in ("http://example.com/a", "https://example.com/b?q=1"):
        clean = sanitize_prosemirror(_doc_with_link("link", good_href))
        node = _find_text(clean, "link")
        assert node is not None
        assert node.get("marks") == [{"type": "link", "attrs": {"href": good_href}}], good_href


# --- attribute / mark scrubbing ----------------------------------------------------------------------


def test_unknown_attrs_and_marks_are_stripped():
    payload = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"onclick": "steal()", "style": "x"},  # unknown node attrs -> gone
                "content": [
                    {
                        "type": "text",
                        "text": "hi",
                        # strike is not allowlisted; bold is.
                        "marks": [{"type": "bold"}, {"type": "strike"}, {"type": "textStyle"}],
                    }
                ],
            }
        ],
    }
    clean = sanitize_prosemirror(payload)
    para = next(n for n in schema.iter_nodes(clean) if n.get("type") == "paragraph")
    assert "attrs" not in para  # no attrs carried on paragraph
    node = _find_text(clean, "hi")
    assert node is not None
    assert node.get("marks") == [{"type": "bold"}]


def test_heading_level_clamped_and_other_attrs_dropped():
    payload = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 99, "id": "x", "onclick": "y"},
                "content": [{"type": "text", "text": "Title"}],
            },
            {"type": "heading", "attrs": {"level": 0}, "content": [{"type": "text", "text": "Zero"}]},
            {"type": "heading", "content": [{"type": "text", "text": "None"}]},
        ],
    }
    clean = sanitize_prosemirror(payload)
    headings = [n for n in schema.iter_nodes(clean) if n.get("type") == "heading"]
    assert [h["attrs"] for h in headings] == [{"level": 6}, {"level": 1}, {"level": 1}]


# --- structural / defensive shapes -------------------------------------------------------------------


def test_nested_lists_survive_and_only_allowlisted_nodes_remain():
    payload = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "item"}]},
                            {"type": "iframe", "attrs": {"src": "http://evil"}},  # dropped
                        ],
                    }
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [{"type": "text", "text": "x=1"}],
            },
        ],
    }
    clean = sanitize_prosemirror(payload)
    assert _node_types(clean) <= ALLOWED_NODE_TYPES
    assert "iframe" not in _node_types(clean)
    assert _find_text(clean, "item") is not None
    assert _find_text(clean, "x=1") is not None


def test_non_doc_root_becomes_empty_doc():
    assert sanitize_prosemirror({"type": "paragraph", "content": []}) == schema.empty_doc()
    assert sanitize_prosemirror({}) == schema.empty_doc()
    assert sanitize_prosemirror("not a dict") == schema.empty_doc()  # type: ignore[arg-type]


def test_input_is_not_mutated():
    payload = {
        "type": "doc",
        "content": [
            {"type": "html", "html": "<script>x</script>"},
            {"type": "paragraph", "content": [{"type": "text", "text": "keep"}]},
        ],
    }
    import copy

    original = copy.deepcopy(payload)
    sanitize_prosemirror(payload)
    assert payload == original  # the sanitizer returns a copy; it never edits the caller's dict


def test_doc_from_text_output_passes_through_unchanged():
    # A document built by the real helper is already clean, so sanitization is a no-op on shape.
    doc = schema.doc_from_text("first para\n\nsecond para")
    assert sanitize_prosemirror(doc) == doc


# --- sanitize_content_json (per-block convenience) ---------------------------------------------------


def test_sanitize_content_json_applies_per_block():
    blocks = {
        "description": {
            "type": "doc",
            "content": [{"type": "html", "html": "<img src=x onerror=alert(1)>"}],
        },
        "remediation": {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "patch it"}]}],
        },
    }
    clean = sanitize_content_json(blocks)
    assert set(clean) == {"description", "remediation"}
    # Malicious block reduced to an empty (but valid) doc; legit block preserved.
    assert "html" not in _node_types(clean["description"])
    assert clean["description"] == {"type": "doc", "content": []}
    assert _find_text(clean["remediation"], "patch it") is not None


def test_sanitize_content_json_non_mapping_is_empty():
    assert sanitize_content_json(None) == {}  # type: ignore[arg-type]
    assert sanitize_content_json([1, 2, 3]) == {}  # type: ignore[arg-type]


def test_deeply_nested_content_is_bounded_not_a_recursionerror():
    """A write-scoped PAT can POST content nested far deeper than any real finding. json.loads accepts
    it; an uncapped walker would raise RecursionError -> HTTP 500 (a self-inflicted DoS). The walker must
    DROP the over-deep subtree cleanly and never raise (INV-INPUT-02: bounded parse)."""
    from scribble.prosemirror_sanitize import _MAX_DEPTH, sanitize_prosemirror

    # Build a bulletList -> listItem -> bulletList chain far past the cap, with real text at the bottom.
    depth = _MAX_DEPTH * 4
    node: dict = {"type": schema.TEXT, "text": "buried"}
    for i in range(depth):
        wrapper = schema.BULLET_LIST if i % 2 == 0 else schema.LIST_ITEM
        node = {"type": wrapper, "content": [node]}
    doc = {"type": schema.DOC, "content": [node]}

    clean = sanitize_prosemirror(doc)  # must NOT raise

    assert clean["type"] == schema.DOC
    # The buried text sits below the cap, so it is pruned away — the result is finite and safe.
    assert _find_text(clean, "buried") is None


def test_content_at_the_depth_limit_still_survives():
    """A document nested within the cap keeps its content — the guard drops only adversarial depth."""
    from scribble.prosemirror_sanitize import sanitize_prosemirror

    node: dict = {"type": schema.TEXT, "text": "shallow"}
    for i in range(6):  # well within _MAX_DEPTH
        wrapper = schema.BULLET_LIST if i % 2 == 0 else schema.LIST_ITEM
        node = {"type": wrapper, "content": [node]}
    doc = {"type": schema.DOC, "content": [node]}

    clean = sanitize_prosemirror(doc)
    assert _find_text(clean, "shallow") is not None
