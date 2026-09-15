"""Drift guard: scribble mounts the SHARED kit reporting editor and ships no copy of its own.

The whole point of ext#212 was ONE canonical editor, updated in one place. Nothing stops a later change
from re-adding `scribble/static/editor.js` "just for this page", or from pointing a new template at
`scribble.static` — and neither would fail any behavioural test, because a second copy works fine right
up until the two drift. The kit README already laments three hand-rolled drag-reorder implementations;
this is what keeps the editor off that list.

Static source assertions on purpose: what is pinned is which FILE the browser loads, and no rendered-page
test can tell the kit's editor apart from an identical copy of it. The behavioural half — that the page
renders at all, i.e. that the kit blueprint really is what serves those URLs — lives in
`tests/test_board.py::test_finding_detail_get_renders_editor_and_gallery`.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_SCRIBBLE = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = REPO_SCRIBBLE / "scribble"
STATIC = PACKAGE / "static"
TEMPLATES = PACKAGE / "templates" / "scribble"

#: Templates that must load the editor, and must load it before anything that needs it. Derived below
#: rather than hand-listed, so a template added later is covered without anyone remembering this file.
_KIT_ASSET_RE = re.compile(r"""url_for\(\s*['"]lotek_kit\.static['"]\s*,\s*filename=['"]([^'"]+)['"]""")

#: The three templates that mount the editor today. Pinned so DELETING a load site is also caught --
#: a derived-only check passes vacuously when the set it derives becomes empty.
EXPECTED_CONSUMERS = {
    "_editor.html": {"reporting-outbox.js", "reporting-editor.js", "reporting-editor.css"},
    "_gallery.html": {"reporting-outbox.js"},
    # library_detail.html uses ONLY the exported `_internal` JSON<->DOM walkers -- never mount(), never
    # image paste -- so it deliberately carries no outbox: an unused one would open an IndexedDB
    # connection on a page that never uploads. If that page grows paste-upload, add it here.
    "library_detail.html": {"reporting-editor.js"},
}


#: The opening tag of an editor mount element. Scoped to the TAG rather than the file because
#: `_editor.html` also carries `data-api-base` on the Rephrase block, which would satisfy a
#: file-level check after the editor's own attribute was deleted.
_MOUNT_TAG_RE = re.compile(r"<[a-zA-Z][^>]*\bdata-scribble-editor\b[^>]*>")


def _kit_assets(template: pathlib.Path) -> list[str]:
    """Kit assets the template loads, in source order."""
    return _KIT_ASSET_RE.findall(template.read_text(encoding="utf-8"))


def _scanned_sources() -> list[pathlib.Path]:
    """Every scribble source a stale reference could hide in — package AND tests, minus vendored code.

    Tests are in scope deliberately: the e2e suite drives these globals from Playwright, so a rename
    that misses a test leaves an assertion evaluating `undefined` rather than failing honestly.
    """
    here = pathlib.Path(__file__).resolve()
    return [
        p
        for root in (PACKAGE, REPO_SCRIBBLE / "tests")
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".js", ".html", ".md"}
        and "static/lib" not in p.as_posix()
        and p.resolve() != here  # this file names the old globals on purpose
    ]


def test_scribble_ships_no_editor_or_outbox_copy():
    """The kit owns these files now. A copy here is the drift this guard exists to catch."""
    assert not (STATIC / "editor.js").exists()
    assert not (STATIC / "outbox.js").exists()


def test_no_scribble_editor_or_outbox_global_survives():
    """`window.Scribble{Editor,Outbox}` are gone — the kit's globals are the only ones live code uses.

    A leftover reference is not cosmetic: the global genuinely does not exist any more, so the call
    site is dead at runtime and only surfaces when a human clicks that button.
    """
    offenders = [
        f"{p.relative_to(REPO_SCRIBBLE)}:{n}"
        for p in _scanned_sources()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bScribble(Editor|Outbox)\b", line)
    ]
    assert offenders == []


def test_no_template_loads_the_deleted_static_files():
    offenders = [
        f"{p.relative_to(REPO_SCRIBBLE)}:{n}"
        for p in TEMPLATES.rglob("*.html")
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"""scribble\.static['"], *filename=['"](editor|outbox)\.js""", line)
    ]
    assert offenders == []


def test_the_editor_load_sites_are_exactly_the_expected_ones():
    """Derived from the templates, then compared against the pinned set.

    Catches BOTH directions: a new template that mounts the editor (it must be reviewed into this list,
    because whether it also needs the outbox is a judgement call), and a load site that silently
    disappears.
    """
    found = {
        p.name: set(_kit_assets(p))
        for p in sorted(TEMPLATES.rglob("*.html"))
        if _kit_assets(p)
    }
    assert found == EXPECTED_CONSUMERS


@pytest.mark.parametrize("template", sorted(EXPECTED_CONSUMERS))
def test_the_outbox_loads_before_the_editor(template):
    """The editor reads `window.LotekReportingOutbox` at SCRIPT-LOAD time, so a page carrying both must
    load the outbox first. Getting this backwards silently disables image upload; nothing throws."""
    assets = _kit_assets(TEMPLATES / template)
    if "reporting-outbox.js" not in assets or "reporting-editor.js" not in assets:
        pytest.skip(f"{template} does not load both")
    assert assets.index("reporting-outbox.js") < assets.index("reporting-editor.js")


def test_every_editor_mount_site_declares_its_api_base():
    """A mount site without `data-api-base` silently misroutes autosave and evidence upload.

    P1 deliberately dropped the kit editor's `/scribble/api` default so the primitive carries no
    scribble coupling, which means the attribute is now load-bearing rather than an override: it decides
    where a finding's content and its pasted screenshots are POSTed. Omit it and `state.apiBase` is
    undefined, so the editor posts to a garbage relative path — no exception, no failed assertion, just
    autosaves that never land.

    `_editor.html` says all of this in a comment at the top of the file, and a comment is not a guard.
    Adversarial review of #213 flagged it: the branch shipped seven drift guards and none covered the
    one hazard its own prose calls out.

    Asserts on the MOUNT ELEMENT, not on the file. `_editor.html` carries `data-api-base` twice — once
    on the editor and once on the Rephrase-with-AI block — so a file-level substring check passes even
    after the editor's own attribute is deleted. That was the first version of this guard, and it did
    not go red when the attribute was removed.
    """
    offenders = []
    for template in sorted(TEMPLATES.rglob("*.html")):
        for tag in _MOUNT_TAG_RE.findall(template.read_text(encoding="utf-8")):
            if "data-api-base" not in tag:
                offenders.append(template.name)
    assert not offenders, (
        "an element carrying data-scribble-editor declares no data-api-base, so autosave and image "
        f"upload POST to a relative path and silently go nowhere: {offenders}"
    )


def test_editor_partial_uses_the_kit_css_and_data_classes():
    """The kit's CSS and its `readEmbeddedDoc`/`readVariableKeys` both key off these exact names."""
    body = (TEMPLATES / "_editor.html").read_text(encoding="utf-8")
    assert "lotek-reporting-editor-wrap" in body
    assert "lotek-reporting-editor-doc-data" in body
    assert "lotek-reporting-editor-vars-data" in body
    assert not re.search(r"^\s*<style>", body, re.M), (
        "the editor's styling ships with the editor (reporting-editor.css), not inline here"
    )
    # data-scribble-editor stays: it is scribble's MOUNT SELECTOR, not part of the kit's contract.
    assert "data-scribble-editor" in body
    assert "window.LotekReportingEditor.mount(el)" in body
