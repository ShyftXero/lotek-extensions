"""The scoping-prose markup subset — the escape-first property is the point.

Line-item detail lands in a browser preview and in a PDF rendered by a full CSS engine. If a tag an
author typed could survive, a scope description would be a stored-XSS in one and a server-side fetch in
the other.
"""

from __future__ import annotations

import pytest

from cream.markup import plain, render_markup


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "<iframe src='http://evil.test'></iframe>",
        '<a href="javascript:alert(1)">click</a>',
        "<style>body{display:none}</style>",
        "</style><script>alert(1)</script>",
    ],
)
def test_no_author_supplied_tag_survives(hostile):
    out = render_markup(hostile)
    assert "<script" not in out.lower()
    assert "<iframe" not in out.lower()
    assert "onerror" not in out.lower() or "&lt;img" in out
    assert "&lt;" in out  # the angle bracket is present, but escaped


def test_bullets_become_a_list():
    out = render_markup("- 10.0.0.0/24\n- app.example.test")
    assert out == "<ul><li>10.0.0.0/24</li><li>app.example.test</li></ul>"


def test_inline_forms():
    assert render_markup("**in scope**") == "<p><strong>in scope</strong></p>"
    assert render_markup("`10.0.0.0/8`") == "<p><code>10.0.0.0/8</code></p>"
    assert render_markup("*emphasis*") == "<p><em>emphasis</em></p>"


def test_code_span_wins_over_bold_inside_it():
    assert render_markup("`**literal**`") == "<p><code>**literal**</code></p>"


def test_paragraphs_and_line_breaks():
    out = render_markup("one\ntwo\n\nthree")
    assert out == "<p>one<br>two</p><p>three</p>"


def test_mixed_bullets_and_prose_keep_their_order():
    out = render_markup("Included:\n- web\n- api\nExcluded: DoS")
    assert out == "<p>Included:</p><ul><li>web</li><li>api</li></ul><p>Excluded: DoS</p>"


def test_blank_is_empty_not_an_empty_paragraph():
    assert render_markup(None) == ""
    assert render_markup("   \n  ") == ""


def test_plain_collapses_and_escapes():
    assert plain("  a\n  b  ") == "a b"
    assert plain("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
    assert plain(None) == ""
