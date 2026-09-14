"""Drift guard: scribble mounts the SHARED kit reporting editor and ships no copy of its own.

The whole point of ext#212 was ONE canonical editor, updated in one place. Nothing stops a later change
from re-adding `scribble/static/editor.js` "just for this page", or from pointing a new template at
`scribble.static` — and neither would fail any behavioural test, because a second copy works fine right
up until the two drift. The kit README already laments three hand-rolled drag-reorder implementations;
this is what keeps the editor off that list.

Static source assertions on purpose: what is being pinned is which FILE the browser loads, and no
rendered-page test can see the difference between the kit's editor and an identical copy.
"""

from __future__ import annotations

import pathlib
import re

import pytest

SCRIBBLE = pathlib.Path(__file__).resolve().parents[1] / "scribble"
STATIC = SCRIBBLE / "static"
TEMPLATES = SCRIBBLE / "templates" / "scribble"

#: Templates that put the editor or the upload outbox on a page, and the kit assets each must load.
#: `library_detail.html` deliberately has no outbox -- it uses only the exported `_internal` JSON<->DOM
#: walkers, never `mount()` and never image paste, so an outbox there would open an IndexedDB
#: connection for nothing. If that page ever grows paste-upload, add "reporting-outbox.js" here.
KIT_ASSET_CONSUMERS = {
    "_editor.html": ["reporting-outbox.js", "reporting-editor.js"],
    "_gallery.html": ["reporting-outbox.js"],
    "library_detail.html": ["reporting-editor.js"],
}


def _sources() -> list[pathlib.Path]:
    return [
        p
        for p in SCRIBBLE.rglob("*")
        if p.is_file() and p.suffix in {".py", ".js", ".html", ".md"} and "static/lib" not in p.as_posix()
    ]


def test_scribble_ships_no_editor_or_outbox_copy():
    """The kit owns these files now. A copy here is the drift this guard exists to catch."""
    assert not (STATIC / "editor.js").exists()
    assert not (STATIC / "outbox.js").exists()


def test_no_scribble_editor_or_outbox_global_survives():
    """`window.Scribble{Editor,Outbox}` are gone — the kit's globals are the only ones live code uses.

    A leftover reference is not a cosmetic problem: the global genuinely does not exist any more, so the
    call site is dead at runtime and only shows up when a human clicks that button.
    """
    offenders = [
        f"{p.relative_to(SCRIBBLE.parent)}:{n}"
        for p in _sources()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bScribble(Editor|Outbox)\b", line)
    ]
    assert offenders == []


def test_no_template_loads_the_deleted_static_files():
    offenders = [
        f"{p.relative_to(SCRIBBLE.parent)}:{n}"
        for p in TEMPLATES.rglob("*.html")
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"""scribble\.static['"], *filename=['"](editor|outbox)\.js""", line)
    ]
    assert offenders == []


@pytest.mark.parametrize("template,assets", sorted(KIT_ASSET_CONSUMERS.items()))
def test_editor_templates_load_the_kit_assets(template, assets):
    body = (TEMPLATES / template).read_text(encoding="utf-8")
    positions = []
    for asset in assets:
        needle = f"url_for('lotek_kit.static', filename='{asset}')"
        assert needle in body, f"{template} must load {asset} from the kit"
        positions.append(body.index(needle))
    # The editor reads window.LotekReportingOutbox at SCRIPT-LOAD time, so the outbox tag must come
    # first. Getting this backwards silently disables image upload; nothing throws.
    assert positions == sorted(positions), f"{template} loads the kit assets out of order"


def test_editor_partial_uses_the_kit_css_and_data_classes():
    """The kit's CSS and its `readEmbeddedDoc`/`readVariableKeys` both key off these exact names."""
    body = (TEMPLATES / "_editor.html").read_text(encoding="utf-8")
    assert "url_for('lotek_kit.static', filename='reporting-editor.css')" in body
    assert "lotek-reporting-editor-wrap" in body
    assert "lotek-reporting-editor-doc-data" in body
    assert "lotek-reporting-editor-vars-data" in body
    assert not re.search(r"^\s*<style>", body, re.M), (
        "the editor's styling ships with the editor (reporting-editor.css), not inline here"
    )
    # data-scribble-editor stays: it is scribble's MOUNT SELECTOR, not part of the kit's contract.
    assert "data-scribble-editor" in body
    assert "window.LotekReportingEditor.mount(el)" in body
