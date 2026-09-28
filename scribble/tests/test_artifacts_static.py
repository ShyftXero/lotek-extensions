"""The evidence gallery builds its rows from nodes and never hands a string to an HTML parser.

``artifacts.js`` puts a file name, a caption, a server URL and a server error into each gallery row.
Building with ``createElement``, ``textContent`` and ``setAttribute`` leaves nothing to escape, so this
pins it: an HTML sink anywhere in the file fails, including on a constant, so one cannot creep back in
beside a constant and later grow a variable. URL properties fail too: a URL goes in through
``setAttribute`` behind ``safeHref`` / ``safeSrc``. The behaviour is proven in the browser by
``test_e2e_artifacts_dom.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ARTIFACTS = Path(__file__).resolve().parents[1] / "scribble" / "static" / "artifacts.js"

HTML_SINKS = {
    "innerHTML": re.compile(r"\.\s*innerHTML\b|\[\s*[\"']innerHTML[\"']\s*\]"),
    "outerHTML": re.compile(r"\.\s*outerHTML\b|\[\s*[\"']outerHTML[\"']\s*\]"),
    "insertAdjacentHTML": re.compile(r"\binsertAdjacentHTML\b"),
    "document.write": re.compile(r"\bdocument\s*\.\s*write(?:ln)?\b"),
    "createContextualFragment": re.compile(r"\bcreateContextualFragment\b"),
    "DOMParser": re.compile(r"\bDOMParser\b"),
    "srcdoc": re.compile(r"\.\s*srcdoc\b|[\"']srcdoc[\"']"),
    "setHTMLUnsafe": re.compile(r"\bsetHTMLUnsafe\b|\bparseHTMLUnsafe\b"),
}
URL_PROPERTY = re.compile(r"\.\s*(?:href|src)\s*=(?!=)")


def _hits(pattern: re.Pattern[str]) -> list[str]:
    lines = ARTIFACTS.read_text(encoding="utf-8").splitlines()
    return [f"{i}: {line.strip()}" for i, line in enumerate(lines, 1) if pattern.search(line)]


@pytest.mark.parametrize("sink", sorted(HTML_SINKS))
def test_the_gallery_uses_no_html_sink(sink):
    assert _hits(HTML_SINKS[sink]) == [], f"artifacts.js uses {sink}; build the node with createElement"


def test_gallery_urls_go_through_the_allowlist():
    assert _hits(URL_PROPERTY) == [], "set href/src with setAttribute(..., safeHref/safeSrc(...))"
    text = ARTIFACTS.read_text(encoding="utf-8")
    for m in re.finditer(r"setAttribute\(\s*\"(href|src)\"\s*,\s*([^)]*)", text):
        # setArtifactLink's own setAttribute("href", href) sets the value safeHref just returned.
        allowed = {"href": ("safeHref(", "href"), "src": ("safeSrc(",)}[m.group(1)]
        assert m.group(2).startswith(allowed), m.group(0)
    assert re.search(r"const href = safeHref\(url\);", text)


def test_the_sink_patterns_catch_what_they_claim_to():
    """A pattern that matches nothing would make the tests above pass vacuously."""
    samples = {
        "innerHTML": "el.innerHTML = x", "outerHTML": 'el["outerHTML"] = x',
        "insertAdjacentHTML": "el.insertAdjacentHTML('beforeend', x)",
        "document.write": "document.writeln(x)",
        "createContextualFragment": "r.createContextualFragment(x)", "DOMParser": "new DOMParser()",
        "srcdoc": "f.srcdoc = x", "setHTMLUnsafe": "el.setHTMLUnsafe(x)",
    }
    for sink, sample in samples.items():
        assert HTML_SINKS[sink].search(sample), sink
    assert URL_PROPERTY.search("a.href = x") and URL_PROPERTY.search("img.src=x")
    assert not URL_PROPERTY.search("if (a.href == x)")
