"""The kit's ``vector-viewer.{js,css}`` must stay byte-identical to vector's own copies.

Everything under ``/vector/*`` requires a login, so a public page — core's ``/docs``, later the landing
page — cannot load the viewer from there. Core already serves the kit's static directory publicly at
``/_kit/``, so the kit ships an exact copy of the viewer for those pages. Until vector is deleted (#159)
both copies are LIVE: vector's deliverables and editor preview run one, public pages run the other. A
fix applied to one and not the other means a tour behaves differently depending on which page shows it.

Unlike ``test_port_parity.py`` there is no sanctioned divergence — the kit copy is not a port, it is the
same file — so the check is byte-for-byte. It compares against vector's copy in THIS checkout rather than
``origin/main``: the fix-then-sync workflow edits both in one branch, and a branch that edits only one
should fail before it merges. When vector is deleted, the kit copy becomes the only one and this goes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lotek_kit.assets import asset_bytes

KIT_ROOT = Path(__file__).resolve().parent.parent
VECTOR_STATIC = KIT_ROOT.parent / "vector" / "vector" / "static"

VIEWER_ASSETS = ("vector-viewer.js", "vector-viewer.css")


@pytest.mark.parametrize("name", VIEWER_ASSETS)
def test_the_kit_copy_is_byte_identical_to_vectors(name):
    origin = VECTOR_STATIC / name
    if not origin.exists():
        pytest.skip(f"vector/vector/static/{name} is not in this checkout — vector has probably been "
                    "deleted (#159), which is when this whole file should go too")
    assert asset_bytes(name) == origin.read_bytes(), (
        f"lotek_kit/static/{name} drifted from vector/vector/static/{name}. Fix vector's copy, then "
        f"`cp vector/vector/static/{name} kit/lotek_kit/static/{name}`."
    )


@pytest.mark.parametrize("name", VIEWER_ASSETS)
def test_the_viewer_needs_nothing_a_strict_csp_page_would_refuse(name):
    """A public page embeds this under ``script-src 'self'; style-src 'self'``. The behaviour is proven
    by vector's Playwright suite (``vector/tests/test_e2e_tour.py``); this only pins that the shipped
    file reaches for nothing off-origin, evaluates no strings and hands nothing to an HTML parser (the
    full sink list is ``vector/tests/test_viewer_static.py``; this is the part that outlives vector)."""
    # The SVG namespace is an identifier the browser never fetches, not a resource.
    text = asset_bytes(name).decode("utf-8").replace('"http://www.w3.org/2000/svg"', "")
    for needle in ("@import", "http://", "https://", "eval(", "new Function",
                   ".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write"):
        assert needle not in text, f"{name} contains {needle!r}"
