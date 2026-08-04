"""Engagement CRUD on fraction's OWN browser (`bp`) blueprint (`fraction/engagement_ui.py`) — create,
edit, delete + the viewer read-only nudge.

Ported from the deleted lotek `tests/test_engagements_crud.py` (the `/engagements` lotek-level surface
that used to proxy Fraction is gone — CONTRACT.md §6 Track G deletes `routes/engagements.py`; every
one of those routes now lives directly on Fraction's own `bp`, absorbed by Track E). Session-cookie
login + the server-side viewer-403 role gate are the HOST's own concern (`enforce_role_access`,
already proven against a real lotek host); this file proves fraction's OWN CRUD logic + the
`fraction_can_write` UI nudge against the `stub_host` fixture's `current_actor`/`can_write` hooks.
"""

from __future__ import annotations

import fraction.models as fm

UI = "/fraction"


def test_create_edit_delete_engagement(client, stub_host, session_factory):
    with session_factory() as db:
        client_row = fm.Client(name="Women's Health")
        db.add(client_row)
        db.commit()
        cid = client_row.id

    # create — owner_id/created_by stamped from the host's current_actor hook
    resp = client.post(
        f"{UI}/engagements/new",
        data={"name": "Physical Assessment", "client_id": str(cid), "scope_type": "physical"},
    )
    assert resp.status_code == 302
    with session_factory() as db:
        eng = db.query(fm.Engagement).filter_by(name="Physical Assessment").one()
        eid = eng.id
        assert eng.client_id == cid
        assert eng.owner_id == stub_host.current_user.id
        assert eng.created_by == stub_host.current_user.username
        assert eng.scope_type == "physical"

    # edit — rename + change status
    resp = client.post(
        f"{UI}/engagements/{eid}/edit",
        data={"name": "Physical Pentest", "status": "review", "client_id": str(cid)},
    )
    assert resp.status_code == 302
    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert eng.name == "Physical Pentest" and eng.status == "review"

    # list shows it + links to the board
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Physical Pentest" in body
    assert f'href="/fraction/engagements/{eid}"' in body

    # delete
    resp = client.post(f"{UI}/engagements/{eid}/delete")
    assert resp.status_code == 302
    with session_factory() as db:
        assert db.get(fm.Engagement, eid) is None


def test_edit_requires_a_name(client, stub_host, session_factory):
    with session_factory() as db:
        eng = fm.Engagement(name="Keep me", scope_type="external")
        db.add(eng)
        db.commit()
        eid = eng.id
    resp = client.post(f"{UI}/engagements/{eid}/edit", data={"name": ""})
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.get(fm.Engagement, eid).name == "Keep me"  # unchanged


def test_edit_and_delete_missing_engagement_404(client, stub_host):
    assert client.get(f"{UI}/engagements/999999/edit").status_code == 404
    assert client.post(f"{UI}/engagements/999999/edit", data={"name": "x"}).status_code == 404
    assert client.post(f"{UI}/engagements/999999/delete").status_code == 404


def test_delete_cascades_findings(client, stub_host, session_factory):
    with session_factory() as db:
        eng = fm.Engagement(name="Cascade Co", scope_type="external")
        db.add(eng)
        db.commit()
        tmpl = db.query(fm.VulnerabilityTemplate).first()
        finding = fm.EngagementFinding.from_template(tmpl, engagement_id=eng.id, order_index=0)
        db.add(finding)
        db.commit()
        eid, fid = eng.id, finding.id

    resp = client.post(f"{UI}/engagements/{eid}/delete")
    assert resp.status_code == 302
    with session_factory() as db:
        assert db.get(fm.Engagement, eid) is None
        assert db.get(fm.EngagementFinding, fid) is None  # cascaded, not orphaned


# ── viewer read-only nudge (fraction_can_write) ─────────────────────────────────────────────────


def test_viewer_nudge_hides_mutating_form(client, stub_host):
    """A writer sees the create-engagement form; a viewer sees it gated away
    (`fraction_can_write` False), proving the host's `can_write` injection reaches Fraction's
    templates. The REAL enforcement (a viewer's POST is refused) is the host's own role gate --
    already proven end-to-end in the lotek repo."""
    stub_host.can_write_value = True
    body = client.get(f"{UI}/engagements/new").get_data(as_text=True)
    assert "Create engagement" in body

    stub_host.can_write_value = False
    body = client.get(f"{UI}/engagements/new").get_data(as_text=True)
    assert "Create engagement" not in body


def test_engagements_list_edit_delete_controls_gated_on_can_write(client, stub_host, session_factory):
    with session_factory() as db:
        eng = fm.Engagement(name="Gated Co", scope_type="external")
        db.add(eng)
        db.commit()
        eid = eng.id

    stub_host.can_write_value = True
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert f'href="{UI}/engagements/{eid}/edit"' in body

    stub_host.can_write_value = False
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert f'href="{UI}/engagements/{eid}/edit"' not in body
