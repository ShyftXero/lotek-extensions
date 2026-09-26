"""The Report Layout composer UI + its persist route (Task #5).

The engagement page renders the kit section-composer (lotek_kit/static/section-composer.js) with every
report section as a draggable tile; POSTing an order persists a normalized ``ReportBoard.section_order``
that both renderers then honor (via layouts.resolve_section_order / enabled_section_keys).
"""
from __future__ import annotations

import uuid

from scribble.models import ReportBoard
from scribble.reporting.layouts import BLOCK_KEYS, BLOCK_LABELS, enabled_section_keys

API = "/scribble/api"
UI = "/scribble"


def _eng(session_factory) -> uuid.UUID:
    with session_factory() as db:
        eng = ReportBoard(name="Layout Eng", client_id=uuid.uuid7(), company_name="Acme")
        db.add(eng)
        db.commit()
        return eng.id


def test_engagement_page_renders_the_section_composer(client, session_factory):
    eng_id = _eng(session_factory)
    resp = client.get(f"{UI}/engagements/{eng_id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "data-section-composer" in html
    assert f"{API}/engagements/{eng_id}/report/sections" in html  # the save url
    assert "section-composer.js" in html  # the reusable kit widget is loaded (not a scribble-only copy)
    # every block appears as a draggable tile with its human label + at least one preset button
    for key in BLOCK_KEYS:
        assert f'data-key="{key}"' in html
        assert BLOCK_LABELS[key] in html
    assert "data-preset" in html


def test_post_section_order_persists_normalized(client, session_factory):
    eng_id = _eng(session_factory)
    resp = client.post(f"{API}/engagements/{eng_id}/report/sections", json={"order": [
        {"key": "findings", "enabled": True},
        {"key": "summary", "enabled": False},
        {"key": "bogus", "enabled": True},  # unknown -> dropped by the resolver
    ]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    keys = [e["key"] for e in body["order"]]
    assert set(keys) == set(BLOCK_KEYS)  # normalized to the full, valid vocabulary
    assert keys[0] == "findings"  # the saved order is respected
    assert "bogus" not in keys
    with session_factory() as db:
        board = db.get(ReportBoard, eng_id)
        enabled = enabled_section_keys(board.section_order)
        assert enabled[0] == "findings"
        assert "summary" not in enabled  # switched off -> dropped from the render order


def test_post_section_order_rejects_a_non_list(client, session_factory):
    eng_id = _eng(session_factory)
    resp = client.post(f"{API}/engagements/{eng_id}/report/sections", json={"order": "nope"})
    assert resp.status_code == 400


def test_post_section_order_404_for_missing_engagement(client):
    resp = client.post(f"{API}/engagements/{uuid.uuid7()}/report/sections", json={"order": []})
    assert resp.status_code == 404
