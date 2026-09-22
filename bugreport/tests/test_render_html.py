"""bugreport.render_html — ProseMirror-JSON -> sanitized HTML for report bodies.

Pure functions, no app. The security claims (escape by construction, scheme-checked links, artifact-only
images, depth bound) are the point of these tests: the body is attacker-influenced once the machine API
can author one, and it is rendered back into a page an admin reads.
"""

from __future__ import annotations

import json

from bugreport.render_html import body_to_doc, render_body, text_to_doc


def _art(aid):
    return f"/bugreport/api/artifacts/{aid}/raw"


def test_plain_text_body_is_escaped_and_paragraphed():
    html = str(render_body("first line\nsecond line\n\nnew para <script>alert(1)</script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert html.count("<p>") == 2
    assert "first line<br>second line" in html


def test_doc_renders_headings_lists_marks():
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Title"}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "bold", "marks": [{"type": "bold"}]},
                {"type": "text", "text": " plain"},
            ]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "a"}]}]},
            ]},
            {"type": "codeBlock", "content": [{"type": "text", "text": "x = 1 < 2"}]},
        ],
    }
    html = str(render_body(doc))
    assert "<h2>Title</h2>" in html
    assert "<strong>bold</strong> plain" in html
    assert "<ul><li><p>a</p></li></ul>" in html
    assert "<pre><code>x = 1 &lt; 2</code></pre>" in html  # code text is escaped


def test_link_scheme_is_enforced():
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "safe", "marks": [{"type": "link", "attrs": {"href": "https://ok.example/x"}}]},
        {"type": "text", "text": "evil", "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}]},
    ]}]}
    html = str(render_body(doc))
    assert '<a href="https://ok.example/x"' in html
    assert "javascript:" not in html          # the dangerous href is dropped...
    assert ">evil<" in html or "evil" in html  # ...but the text is kept


def test_images_only_from_artifacts_or_same_origin():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "inlineImage", "attrs": {"artifactId": "abc", "alt": "shot"}}]},
        {"type": "paragraph", "content": [{"type": "image", "attrs": {"src": "https://evil.example/track.gif"}}]},
        {"type": "paragraph", "content": [{"type": "image", "attrs": {"src": "//evil.example/track.gif"}}]},
        {"type": "paragraph", "content": [{"type": "image", "attrs": {"src": "/bugreport/api/artifacts/z/raw"}}]},
    ]}
    html = str(render_body(doc, artifact_url=_art))
    assert 'src="/bugreport/api/artifacts/abc/raw"' in html  # inline image -> our artifact url
    assert "evil.example" not in html                        # external AND protocol-relative dropped
    assert 'src="/bugreport/api/artifacts/z/raw"' in html    # same-origin relative image kept


def test_deeply_nested_document_is_bounded_not_a_stack_overflow():
    node = {"type": "paragraph", "content": [{"type": "text", "text": "deep"}]}
    for _ in range(500):
        node = {"type": "blockquote", "content": [node]}
    html = str(render_body({"type": "doc", "content": [node]}))  # must not raise
    assert html.count("<blockquote>") <= 41  # depth-bounded (_MAX_DEPTH + the top)


def test_body_to_doc_roundtrips_plain_and_json():
    d = text_to_doc("one\ntwo")
    assert d["type"] == "doc"
    assert d["content"][0]["content"][0]["text"] == "one"
    # a JSON-string body is recognized as a doc, not re-wrapped as text
    stored = json.dumps({"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]})
    assert body_to_doc(stored)["content"][0]["content"][0]["text"] == "hi"
    # a legacy plain-text body becomes a paragraph doc
    assert body_to_doc("legacy")["content"][0]["content"][0]["text"] == "legacy"
