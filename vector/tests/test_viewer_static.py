"""The viewer runtime builds its DOM from nodes and never hands a string to an HTML parser.

``vector-viewer.js`` renders a model that may be hand-written, un-normalized JSON on a public,
unauthenticated page (the kit serves a byte-identical copy at ``/_kit/``). Escaping every field at every
``innerHTML`` call site is a promise one missed ``esc()`` breaks; building with ``createElement`` /
``createElementNS``, ``textContent`` and ``setAttribute`` leaves nothing to escape. This pins that: an
HTML sink anywhere in the file fails, including on a constant, so one cannot creep back in beside a
constant and later grow a variable. The behaviour is proven in the browser by
``test_e2e_tour.py::test_no_model_string_is_ever_parsed_as_markup``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

VIEWER = Path(__file__).resolve().parent.parent / "vector" / "static" / "vector-viewer.js"

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


@pytest.mark.parametrize("sink", sorted(HTML_SINKS))
def test_the_viewer_uses_no_html_sink(sink):
    lines = VIEWER.read_text(encoding="utf-8").splitlines()
    hits = [f"{i}: {line.strip()}" for i, line in enumerate(lines, 1) if HTML_SINKS[sink].search(line)]
    assert hits == [], f"vector-viewer.js uses {sink}; build the node with createElement / textContent"


def test_the_sink_patterns_catch_what_they_claim_to():
    """A pattern that matches nothing would make the test above pass vacuously."""
    samples = {
        "innerHTML": "el.innerHTML = x", "outerHTML": 'el["outerHTML"] = x',
        "insertAdjacentHTML": "el.insertAdjacentHTML('beforeend', x)",
        "document.write": "document.writeln(x)",
        "createContextualFragment": "r.createContextualFragment(x)", "DOMParser": "new DOMParser()",
        "srcdoc": "f.srcdoc = x", "setHTMLUnsafe": "el.setHTMLUnsafe(x)",
    }
    for sink, sample in samples.items():
        assert HTML_SINKS[sink].search(sample), sink
