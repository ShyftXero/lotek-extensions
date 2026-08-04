"""Checklist UI pages render (behaviour is covered by test_checklists_api). Smoke-level: the library
page and the engagement coverage panel are present and wired to their JS + the API."""

from __future__ import annotations

from fraction.models import Engagement


def test_library_page_renders(client):
    resp = client.get("/fraction/checklists")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="checklist-library"' in html
    assert "checklists_library.js" in html
    assert "New / Import" in html


def test_engagement_page_has_coverage_panel(client, session_factory):
    with session_factory() as db:
        e = Engagement(name="Panel Eng")
        db.add(e)
        db.commit()
        eid = e.id
    resp = client.get(f"/fraction/engagements/{eid}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'id="checklist-panel"' in html
    assert f'data-engagement-id="{eid}"' in html
    assert "checklists.js" in html
    assert ">Coverage<" in html


def test_panel_js_served(client):
    assert client.get("/fraction/static/checklists.js").status_code == 200
    assert client.get("/fraction/static/checklists_library.js").status_code == 200


def test_nav_has_checklists_link(client):
    html = client.get("/fraction/checklists").get_data(as_text=True)
    assert "☑ Checklists" in html
