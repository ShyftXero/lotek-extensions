"""Select all / Select none on the findings board — rendered markup + wiring contract.

The operator used to tick every finding by pasting a browser-console one-liner
(``document.querySelectorAll('.scribble-finding-check').forEach(c => c.click())``). This file pins the
control that replaces it, on the three surfaces where it can break:

* **the rendered page** carries a labelled tri-state master and a Select none button, inside the
  write-gated board panel, next to the same ``.scribble-finding-check`` boxes the bulk bar reads. Rows
  are read off a real render (``client.get``), not the template text, so a broken ``{% if %}`` fails here.
* **board.js** routes every selection path through the control and never assigns ``.checked`` on a row
  box. An assignment moves the pixels without firing ``change``, so the bulk bar would stay at 0 and the
  bug would look fixed while still being un-clickable-as-selection.
* **the helper's behaviour** is executed by ``tests/js/select_all_none_check.mjs`` through node, against
  a stub DOM: rows get real clicks (so listeners run), already-correct boxes are not re-toggled, disabled
  rows are skipped, and a partial selection reports mixed.

What this does NOT prove is how it looks, or that a click feels right in the browser: this host is on
the denied list. The rendering pass (desktop + narrow, keyboard reach, mixed-state dot) belongs to a
BLADE pass on an hhr node.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scribble.models import BoardFinding

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scribble" / "templates" / "scribble" / "engagement.html"
BOARD_JS = ROOT / "scribble" / "static" / "board.js"
HELPER = ROOT / "scribble" / "static" / "select-all.js"
JS_CHECK = Path(__file__).resolve().parent / "js" / "select_all_none_check.mjs"

UI = "/scribble"


def _seed_board(db):
    from test_board import _make_engagement, _make_group, _make_template

    eng = _make_engagement(db)
    group = _make_group(db, eng, "Internal")
    tmpl = _make_template(db, "Weak TLS Config")
    db.add_all(
        [
            BoardFinding.from_template(tmpl, engagement_id=eng.id, group_id=group.id, order_index=0),
            BoardFinding.from_template(tmpl, engagement_id=eng.id, group_id=group.id, order_index=1),
        ]
    )
    db.commit()
    return eng.id


def test_the_board_renders_the_select_all_and_none_control(client, session_factory):
    with session_factory() as db:
        eng_id = _seed_board(db)
    body = client.get(f"{UI}/engagements/{eng_id}").data.decode()

    # The rows the control acts on, and the two controls themselves.
    assert body.count('class="scribble-finding-check"') == 2
    assert 'id="scribble-select-all"' in body
    assert 'aria-label="Select all findings on the board"' in body
    assert 'aria-checked="false"' in body, "the mixed state must be announceable before any tick"
    assert 'id="scribble-select-none"' in body
    assert 'id="scribble-select-count"' in body

    # Select none ships inert: with nothing selected it must not read as a live control.
    none_tag = re.search(r'<button[^>]*id="scribble-select-none"[^>]*>', body)
    assert none_tag and "disabled" in none_tag.group(0)

    # The helper loads BEFORE the board script that builds the control on it.
    assert body.index("select-all.js") < body.index("board.js")


def test_the_selection_count_chip_is_a_written_live_region(client, session_factory):
    """The chip must be announced. An unwritten role=status region stays silent and looks broken."""
    with session_factory() as db:
        eng_id = _seed_board(db)
    body = client.get(f"{UI}/engagements/{eng_id}").data.decode()
    chip = re.search(r"<span[^>]*id=\"scribble-select-count\"[^>]*>", body)
    assert chip, "the count chip must render"
    assert 'role="status"' in chip.group(0) and 'aria-live="polite"' in chip.group(0)

    # It is wired: the control writes "N of M selected", the node harness executes that write.
    js = BOARD_JS.read_text()
    assert 'count: "scribble-select-count",' in js, (
        "the chip is rendered but never written -- it would sit at '0 of 0 selected'"
    )
    assert "countEls.forEach" in HELPER.read_text()


def test_a_missing_helper_fails_loud_instead_of_going_silent():
    js = BOARD_JS.read_text()
    assert "if (window.LotekSelectAll) {" in js and "} else {" in js
    assert "select-all.js did not load" in js
    assert "Select all unavailable -- reload the page" in js


def test_the_control_is_absent_for_a_read_only_viewer(client, session_factory, app):
    cfg = app.extensions["scribble"]
    with session_factory() as db:
        eng_id = _seed_board(db)
    cfg.extras["can_write"] = lambda: False
    try:
        body = client.get(f"{UI}/engagements/{eng_id}").data.decode()
        assert 'id="scribble-select-all"' not in body
        assert "You have read-only access" in body
    finally:
        cfg.extras.pop("can_write", None)


def test_board_js_drives_the_control_instead_of_assigning_checked():
    js = BOARD_JS.read_text()
    assert 'window.LotekSelectAll({' in js, "the board must build the shared control"
    assert 'master: "scribble-select-all",' in js
    assert 'none: "scribble-select-none",' in js
    assert 'document.querySelectorAll(".scribble-finding-check")' in js, (
        "the control must be handed the same boxes the bulk bar counts"
    )
    # Nothing may batch-assign a row box: that fires no listener, so the bulk bar would not update.
    batch_assignments = [
        line.strip()
        for line in js.splitlines()
        if re.search(r"\.checked\s*=[^=]", line) and "scribble-finding-check" in line
    ]
    assert batch_assignments == []
    # The bar's own Clear goes through the control, so the tri-state moves with it.
    assert "if (selCtl) selCtl.apply(false);" in js
    # And the bulk bar re-syncs the control on every selection change.
    assert "if (selCtl) selCtl.sync();" in js


def test_the_helper_never_assigns_checked_on_a_row_box():
    text = HELPER.read_text()
    assignments = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"\.checked\s*=[^=]", line) and not line.strip().startswith("//")
    ]
    assert assignments == ['m.checked = state === "true";'], (
        "select-all.js must only assign its own master; a row box is clicked: " + repr(assignments)
    )
    assert "cb.click()" in text
    assert "if (cb.checked === state) return;" in text, "already-correct boxes must not be re-toggled"
    assert "!cb.disabled" in text, "a disabled row is never selected"
    assert 'm.setAttribute("aria-checked", state);' in text


def test_the_control_drives_the_listeners_for_both_actions():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; run `node tests/js/select_all_none_check.mjs` where it is")
    proc = subprocess.run(
        [node, str(JS_CHECK)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert proc.returncode == 0, f"select_all_none_check failed:\n{proc.stdout}\n{proc.stderr}"
