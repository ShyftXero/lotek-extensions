"""Guards on the shipped reorder assets.

There is no JS test runner in this repo, so these are deliberately *static* checks on the file's
contents. They cannot prove the module works; they can prove it has not acquired the properties that
would make it unshippable — a CDN reference in a CSP-strict app, a stray global, a missing export the
consumers' markup depends on.

The behavioural coverage lands where the consumers are converted (#153): scribble already runs
Playwright, so the drag and the arrow buttons get exercised against a real DOM there rather than being
simulated here.
"""

from __future__ import annotations

import re

import pytest

from lotek_kit.assets import asset_text

JS = asset_text("reorder.js")
CSS = asset_text("reorder.css")


@pytest.mark.parametrize("name", ["moveItem", "reorderByKey", "indexOfKey", "attach", "arrows"])
def test_the_public_api_is_exported(name):
    """Consumers' markup and call sites depend on these names. Renaming one is a silent breakage in
    the browser, so the rename has to fail here first."""
    assert re.search(rf"\b{name}:\s*{name}\b", JS), f"{name} missing from the exported api object"


def test_exactly_one_global_is_created():
    """Three copies of this logic became three globals. One frozen object is the whole point."""
    globals_assigned = re.findall(r"global\.(\w+)\s*=", JS)
    assert globals_assigned == ["lotekReorder"]


def test_the_api_object_is_frozen():
    assert "Object.freeze(api)" in JS


def test_no_external_resource_is_referenced():
    """scribble is CSP-strict and takes no CDN scripts. An asset that reaches out is unshippable, and
    the failure would show up as a blocked request in a browser console nobody is watching."""
    for text, label in ((JS, "reorder.js"), (CSS, "reorder.css")):
        assert not re.search(r"https?://", text), f"{label} references an external URL"
        assert "@import" not in text, f"{label} pulls in another stylesheet"


def test_no_dynamic_code_evaluation():
    assert not re.search(r"\beval\s*\(", JS)
    assert not re.search(r"new\s+Function\s*\(", JS)


def test_the_module_persists_nothing_itself():
    """The three surfaces sit behind different routes and different auth, so the caller owns the
    request. A fetch in here would be the module quietly deciding for all of them."""
    assert "fetch(" not in JS
    assert "XMLHttpRequest" not in JS


def test_the_keyboard_path_uses_real_buttons():
    """HTML5 drag-and-drop fires on neither a keyboard nor a touchscreen. Real <button> elements are
    natively focusable and activatable; a div with a click handler is not."""
    assert 'createElement("button")' in JS
    assert 'element.type = "button"' in JS, "must not default to submit — these live inside forms"
    assert 'setAttribute("aria-label"' in JS


def test_the_drag_handle_is_not_presented_as_a_control():
    """The handle is decorative; the arrow buttons are the accessible path. A focusable handle that
    does nothing on Enter is worse than one that is plainly decoration."""
    assert "cursor: grab" in CSS
    assert ".lk-handle" in CSS


def test_focus_is_visible_on_the_keyboard_controls():
    assert ".lk-move:focus-visible" in CSS


def test_every_css_class_is_namespaced():
    """The kit's stylesheet loads alongside core's and scribble's. An un-prefixed class here would
    silently restyle whatever else happened to use that name."""
    classes = set(re.findall(r"\.([a-zA-Z][\w-]*)", CSS))
    unprefixed = {name for name in classes if not name.startswith("lk-")}
    assert unprefixed == set(), f"un-namespaced CSS classes: {unprefixed}"
