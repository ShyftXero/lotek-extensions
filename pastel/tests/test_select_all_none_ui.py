"""Select all / Select none on the "Your Pastels" bulk-delete list.

Pastel already had a "select all" checkbox; what it did not have was any way to CLEAR a selection, and
its batch was written as ``c.checked = all.checked``, which fires no ``change`` listener. The control is
pinned here on two surfaces:

* **the rendered page** carries a labelled tri-state master plus a Select none button, and the per-paste
  boxes the form POSTs (``name="ids"``) are the same ones it acts on.
* **the inline wiring** clicks each row box instead of assigning ``.checked``, skips boxes already in the
  wanted state, keeps ``aria-checked`` in step, and disables Select none at zero selection.

Not covered here: how it looks and how the click feels. This host is on the denied list, so the
rendering pass belongs to a BLADE run on an hhr node.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "pastel" / "templates" / "pastel" / "list.html"
)


def _body(client, session_factory, actor, count=2):
    from pastel.models import Paste

    with session_factory() as db:
        for i in range(count):
            db.add(Paste(title=f"paste {i}", body="x", author_id=actor.id))
        db.commit()
    return client.get("/pastel/").data.decode()


def test_the_list_renders_the_select_all_and_none_control(client, session_factory, hooks):
    body = _body(client, session_factory, hooks["actor"])

    assert 'class="px-sel"' in body and body.count('class="px-sel"') == 2
    assert 'id="px-all"' in body
    assert 'aria-label="Select all pastes"' in body
    assert 'aria-checked="false"' in body, "the mixed state must be announceable before any tick"
    assert 'id="px-none"' in body and "Select none" in body

    # Inert until something is selected: a live-looking Select none on an empty selection is a lie.
    none_tag = re.search(r'<button[^>]*id="px-none"[^>]*>', body)
    assert none_tag and "disabled" in none_tag.group(0)
    # type=button: it must never submit the bulk-delete form it sits inside.
    assert 'type="button"' in none_tag.group(0)


def test_the_wiring_clicks_rows_and_never_assigns_checked():
    text = TEMPLATE.read_text()
    assert "function applyAll(state, onAfterBatch)" in text
    assert "c.click();" in text, "a row box is clicked so its change listener runs"
    assert "if (c.checked === state) return;" in text, "already-correct boxes are not re-toggled"
    assert 'all.setAttribute("aria-checked", state);' in text
    assert 'none.addEventListener("click", function () { applyAll(false); });' in text
    assert 'all.addEventListener("change", function () { applyAll(all.checked); });' in text
    assert 'bulk.querySelectorAll(".px-sel")' in text, "the control acts on the POSTed row boxes"

    # No batch assignment anywhere on the row boxes.
    bad = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"\.px-sel[^\n]*\.checked\s*=(?!=)", line)
        or re.search(r"c\.checked\s*=(?!=)", line)
    ]
    assert bad == [], f"row boxes must be driven with .click(), not assigned: {bad}"


def test_the_inline_helper_holds_the_shared_contract():
    """Same shape as core/scribble's shared control: isApplying, onAfterBatch, one sync of the tri-state.

    Pastel ships no static asset route, so this block is inline rather than loaded -- which makes drift
    from the shared contract the one failure mode worth pinning.
    """
    text = TEMPLATE.read_text()
    assert "var applying = false;" in text, "a row listener must be able to stay quiet during a batch"
    assert "if (onAfterBatch) onAfterBatch(changed);" in text, "the batch reports once"
    assert "function rowChanged() {\n      if (applying) return;" in text
    assert "return n;" in text, "syncAll reports the count it computed"
    assert 'all.setAttribute("aria-checked", state);' in text
