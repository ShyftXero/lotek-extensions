"""Guards on the shipped SHARED reporting-editor assets (reporting-editor.js/.css + reporting-outbox.js).

No JS runner here, so these are static checks — they cannot prove the editor works (that lands where a
consumer runs it: scribble's Playwright suite once it adopts the kit copy, #P3), but they prove the
primitive has not re-acquired scribble coupling and keeps the API its consumers' markup depends on.

The whole point of moving this into the kit is ONE canonical copy: the primitive must be extension-neutral
(no `window.Scribble*` global, no hardcoded extension endpoint), so a second consumer configures it rather
than forking it.
"""

from __future__ import annotations

import re

import pytest

from lotek_kit.assets import asset_text

EDITOR = asset_text("reporting-editor.js")
OUTBOX = asset_text("reporting-outbox.js")
CSS = asset_text("reporting-editor.css")


@pytest.mark.parametrize("text,label", [
    (EDITOR, "reporting-editor.js"), (OUTBOX, "reporting-outbox.js"), (CSS, "reporting-editor.css"),
])
def test_asset_is_plain_text(text, label):
    offenders = {f"U+{ord(c):04X}@{i}" for i, c in enumerate(text) if ord(c) < 0x20 and c not in "\n\t"}
    assert offenders == set(), f"{label} has control characters: {sorted(offenders)}"


def test_kit_neutral_globals_are_created():
    """The primitive exposes kit-neutral globals; a consumer configures via mount(), never a fork."""
    assert re.search(r"window\.LotekReportingEditor\s*=", EDITOR)
    assert "window.LotekReportingOutbox" in OUTBOX
    # the TipTap drop-in seam is read under the kit-neutral name
    assert "window.LotekReportingEditorTipTap" in EDITOR


def test_no_scribble_global_survives_the_extraction():
    """The drift guard: the shared primitive must not create or depend on a `window.Scribble*` global —
    that would re-couple the one canonical copy to a single extension. (Descriptive comment mentions of
    'scribble' are fine; a `window.Scribble<Word> =` assignment or read is not.)"""
    for text, label in ((EDITOR, "reporting-editor.js"), (OUTBOX, "reporting-outbox.js")):
        assert not re.search(r"window\.Scribble\w+", text), f"{label} still wires a window.Scribble* global"


def test_the_mount_api_is_exported():
    assert re.search(r"\bmount:\s*mount\b", EDITOR)
    for name in ("enqueueUpload", "pendingCount", "flush"):
        assert re.search(rf"\b{name}:", OUTBOX), f"{name} missing from the outbox api"


@pytest.mark.parametrize("text,label", [(EDITOR, "reporting-editor.js"), (OUTBOX, "reporting-outbox.js")])
def test_upload_endpoint_has_no_baked_in_extension_default(text, label):
    """No `/scribble` path may be a live DEFAULT in either the editor or the upload outbox — the host
    passes apiBase and the upload endpoint is `apiBase + "/artifacts"`. It may only appear in a comment
    (documenting what scribble passes)."""
    for m in re.finditer(r"/scribble\b", text):
        line = text[text.rfind("\n", 0, m.start()) + 1: m.start()]
        assert line.lstrip().startswith(("//", "*")), f"{label}: /scribble appears in live code, not a comment"


def test_no_external_resource_or_dynamic_eval():
    for text, label in ((EDITOR, "reporting-editor.js"), (OUTBOX, "reporting-outbox.js"), (CSS, "reporting-editor.css")):
        assert not re.search(r"https?://", text), f"{label} references an external URL"
    assert "@import" not in CSS
    assert not re.search(r"\beval\s*\(|new\s+Function\s*\(", EDITOR)


def test_editor_css_uses_host_theme_tokens_and_editor_namespace():
    assert "var(--" in CSS, "editor CSS must theme off the host CSS variables"
    # Scan selector rules only (drop the /* header */ so its `.js`/`.css` filename mentions don't count).
    rules = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    classes = set(re.findall(r"\.([a-zA-Z][\w-]*)", rules))
    # `pill` is the host's shared chip class, reused in the compound `.fr-var.pill` on purpose.
    stray = {c for c in classes if not (c.startswith("fr-") or c.startswith("lotek-reporting") or c == "pill")}
    assert stray == set(), f"un-namespaced editor CSS classes: {stray}"
