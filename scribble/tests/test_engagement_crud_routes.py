"""ReportBoard CRUD on scribble's OWN browser (`bp`) blueprint (`scribble/engagement_ui.py`) — create,
edit, delete + the viewer read-only nudge.

Ported from the deleted lotek `tests/test_engagements_crud.py` (the `/engagements` lotek-level surface
that used to proxy Scribble is gone — CONTRACT.md §6 Track G deletes `routes/engagements.py`; every
one of those routes now lives directly on Scribble's own `bp`, absorbed by Track E). Session-cookie
login + the server-side viewer-403 role gate are the HOST's own concern (`enforce_role_access`,
already proven against a real lotek host); this file proves scribble's OWN CRUD logic + the
`scribble_can_write` UI nudge against the `stub_host` fixture's `current_actor`/`can_write` hooks.
"""

from __future__ import annotations

import uuid

import scribble.models as fm

_MISSING_ID = uuid.uuid7()  # a well-formed id that is not in the table

UI = "/scribble"


def test_create_edit_delete_engagement(client, stub_host, session_factory):
    with session_factory() as db:
        client_row = fm.Client(name="Women's Health")
        db.add(client_row)
        db.commit()
        cid = client_row.id

    # create — the ONLY create path is Import-to-Scribble (standalone create is retired). Name + client
    # DERIVE from the core engagement summary; owner_id/created_by stamp from the host's current_actor.
    core = uuid.uuid7()
    stub_host.engagement_summaries_value = [
        {"id": core, "name": "Physical Assessment", "client_id": cid, "client_name": "Women's Health"}
    ]
    resp = client.post(f"{UI}/engagements/import", data={"core_engagement_id": str(core)})
    assert resp.status_code == 302
    with session_factory() as db:
        eng = db.query(fm.ReportBoard).filter_by(name="Physical Assessment").one()
        eid = eng.id
        assert eng.client_id == cid
        assert eng.core_engagement_id == core
        assert eng.owner_id == stub_host.current_user.id
        assert eng.created_by == stub_host.current_user.username

    # edit — rename + change status + set strategic recommendations (one per textarea line, blanks dropped)
    resp = client.post(
        f"{UI}/engagements/{eid}/edit",
        data={
            "name": "Physical Pentest",
            "status": "review",
            "client_id": str(cid),
            "strategic_recommendations": "Adopt MFA\n\n  Patch program  \n",
        },
    )
    assert resp.status_code == 302
    with session_factory() as db:
        eng = db.get(fm.ReportBoard, eid)
        assert eng.name == "Physical Pentest" and eng.status == "review"
        assert eng.strategic_recommendations == ["Adopt MFA", "Patch program"]
    # the edit page GET renders the recs back into the textarea (one per line)
    edit_body = client.get(f"{UI}/engagements/{eid}/edit").get_data(as_text=True)
    assert "Adopt MFA\nPatch program" in edit_body

    # list shows it + links to the board
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Physical Pentest" in body
    assert f'href="/scribble/engagements/{eid}"' in body

    # delete
    resp = client.post(f"{UI}/engagements/{eid}/delete")
    assert resp.status_code == 302
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eid) is None


def test_edit_requires_a_name(client, stub_host, session_factory):
    with session_factory() as db:
        eng = fm.ReportBoard(name="Keep me", scope_type="external")
        db.add(eng)
        db.commit()
        eid = eng.id
    resp = client.post(f"{UI}/engagements/{eid}/edit", data={"name": ""})
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eid).name == "Keep me"  # unchanged


def test_edit_and_delete_missing_engagement_404(client, stub_host):
    assert client.get(f"{UI}/engagements/{_MISSING_ID}/edit").status_code == 404
    assert client.post(f"{UI}/engagements/{_MISSING_ID}/edit", data={"name": "x"}).status_code == 404
    assert client.post(f"{UI}/engagements/{_MISSING_ID}/delete").status_code == 404


def test_delete_cascades_findings(client, stub_host, session_factory):
    with session_factory() as db:
        eng = fm.ReportBoard(name="Cascade Co", scope_type="external")
        db.add(eng)
        db.commit()
        tmpl = db.query(fm.VulnerabilityTemplate).first()
        finding = fm.BoardFinding.from_template(tmpl, engagement_id=eng.id, order_index=0)
        db.add(finding)
        db.commit()
        eid, fid = eng.id, finding.id

    resp = client.post(f"{UI}/engagements/{eid}/delete")
    assert resp.status_code == 302
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eid) is None
        assert db.get(fm.BoardFinding, fid) is None  # cascaded, not orphaned


# ── viewer read-only nudge (scribble_can_write) ─────────────────────────────────────────────────


def test_viewer_nudge_hides_import_control(client, stub_host):
    """A writer sees the Import-to-Scribble control on a not-yet-imported core engagement; a viewer sees
    it gated away (`scribble_can_write` False), proving the host's `can_write` injection reaches
    Scribble's templates. The REAL enforcement (a viewer's POST is refused) is the host's own role gate --
    already proven end-to-end in the lotek repo. (The standalone create form is retired; Import is the
    only create control.)"""
    core = uuid.uuid7()
    stub_host.engagement_summaries_value = [
        {"id": core, "name": "Importable", "client_id": uuid.uuid7(), "client_name": "C"}
    ]
    stub_host.can_write_value = True
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Import to Scribble" in body

    stub_host.can_write_value = False
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Import to Scribble" not in body


def test_engagements_list_edit_delete_controls_gated_on_can_write(client, stub_host, session_factory):
    # The list is a VIEW over core summaries, so a board appears only when a summary carries its core id.
    core = uuid.uuid7()
    with session_factory() as db:
        eng = fm.ReportBoard(name="Gated Co", scope_type="external", core_engagement_id=core)
        db.add(eng)
        db.commit()
        eid = eng.id
    stub_host.engagement_summaries_value = [
        {"id": core, "name": "Gated Co", "client_id": uuid.uuid7(), "client_name": "C"}
    ]

    stub_host.can_write_value = True
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert f'href="{UI}/engagements/{eid}/edit"' in body

    stub_host.can_write_value = False
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert f'href="{UI}/engagements/{eid}/edit"' not in body


# ── threat-intel egress consent (lotek#642) ─────────────────────────────────────────────────────


def test_edit_toggles_threat_intel_consent(client, stub_host, session_factory):
    """The edit form is the only writer of `threat_intel_egress_consent`: a checked box opts the
    engagement into KEV/EPSS enrichment, an absent box clears it. Default is off."""
    with session_factory() as db:
        c = fm.Client(name="TI Client")
        db.add(c)
        db.commit()
        cid = c.id
        eng = fm.ReportBoard(name="TI Co", scope_type="external", client_id=cid)
        db.add(eng)
        db.commit()
        eid = eng.id
        assert eng.threat_intel_egress_consent is False  # off by default

    base = {"name": "TI Co", "client_id": str(cid)}
    # box checked -> consent on
    resp = client.post(
        f"{UI}/engagements/{eid}/edit", data={**base, "threat_intel_egress_consent": "on"}
    )
    assert resp.status_code == 302
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eid).threat_intel_egress_consent is True

    # box absent (unchecked HTML checkboxes send no key) -> consent cleared back off
    resp = client.post(f"{UI}/engagements/{eid}/edit", data=base)
    assert resp.status_code == 302
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eid).threat_intel_egress_consent is False


def test_edit_page_reflects_threat_intel_consent(client, stub_host, session_factory):
    """A consenting engagement renders the box checked; a non-consenting one renders it unchecked —
    the box is this page's only checkbox, so `checked` in the body tracks exactly this field."""
    stub_host.can_write_value = True
    with session_factory() as db:
        c = fm.Client(name="TI Client 2")
        db.add(c)
        db.commit()
        cid = c.id
        on = fm.ReportBoard(name="On", scope_type="external", client_id=cid,
                           threat_intel_egress_consent=True)
        off = fm.ReportBoard(name="Off", scope_type="external", client_id=cid,
                            threat_intel_egress_consent=False)
        db.add_all([on, off])
        db.commit()
        on_id, off_id = on.id, off.id

    on_body = client.get(f"{UI}/engagements/{on_id}/edit").get_data(as_text=True)
    off_body = client.get(f"{UI}/engagements/{off_id}/edit").get_data(as_text=True)
    assert 'name="threat_intel_egress_consent"' in on_body
    assert 'name="threat_intel_egress_consent"' in off_body
    assert "checked" in on_body and "checked" not in off_body
