"""render.render_deliverable — self-containment + XSS-safe model embedding."""

from __future__ import annotations

import json
import re

from vector.render import json_for_script, render_deliverable
from vector.seed import _model

_EXTERNAL = [r'src\s*=\s*["\']https?:', r"<link\b", r"@import", r'url\(\s*["\']?https?:', r"<script\s+src="]
_LS = " "  # JS line separator — illegal outside strings, must be escaped in the JSON island
_PS = " "


def _extract_model_block(html: str) -> str:
    return html.split('id="vap-model">', 1)[1].split("</script>", 1)[0]


def test_deliverable_is_self_contained():
    html = render_deliverable(_model(), title="T")
    for pat in _EXTERNAL:
        assert re.search(pat, html, re.I) is None, f"external resource load: {pat}"
    assert "__VECTOR_MODEL__" in html
    assert "VectorViewer" in html  # runtime inlined


def test_model_embedding_neutralizes_script_breakout():
    evil = {
        "meta": {"title": "</script><script>alert(1)</script>"},
        "zones": [{"id": "z", "title": "</script>", "accent": "red"}],
        "nodes": [{"id": "a", "zone": "z", "label": "<img src=x onerror=alert(1)>" + _LS + _PS}],
        "edges": [], "phases": [{"n": 1, "title": "</script>", "desc": "  "}],
    }
    html = render_deliverable(evil, title="</script><script>alert(1)</script>")
    block = _extract_model_block(html)
    assert "</script" not in block.lower()
    assert "<" not in block and ">" not in block
    assert _LS not in block and _PS not in block


def test_json_for_script_roundtrips_semantically():
    obj = {"a": "</script>", "b": "x" + _LS + "y", "c": ["<", ">", "&", _PS]}
    encoded = json_for_script(obj)
    assert "</script" not in encoded.lower()
    assert _LS not in encoded and _PS not in encoded
    # the escaped form still parses back to the original values
    assert json.loads(encoded) == obj


def test_title_is_html_escaped_in_head():
    html = render_deliverable({"meta": {"title": "x"}}, title='<b>t</b>&"')
    head = html.split("</head>", 1)[0]
    assert "<b>t</b>" not in head
    assert "&lt;b&gt;" in head
