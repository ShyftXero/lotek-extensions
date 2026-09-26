"""Saved section presets (#Q10): operators save a report SECTION arrangement (order + on/off) to an org-wide
library offered in the composer's preset combobox. The (order, visibility) fingerprint is UNIQUE, so two
operators can't save the identical sequence twice.
"""
from __future__ import annotations

import uuid

import scribble.models as fm
from scribble.reporting.layouts import section_fingerprint

API = "/scribble/api"
UI = "/scribble"


def _eng(session_factory) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name="Preset Eng", client_id=uuid.uuid7(), company_name="Acme")
        db.add(eng)
        db.commit()
        return eng.id


def test_fingerprint_distinguishes_order_and_visibility():
    base = [{"key": "cover", "enabled": True}, {"key": "findings", "enabled": True}]
    assert section_fingerprint(base) == section_fingerprint(list(base))  # stable
    reordered = [{"key": "findings", "enabled": True}, {"key": "cover", "enabled": True}]
    assert section_fingerprint(reordered) != section_fingerprint(base)   # order matters
    hidden = [{"key": "cover", "enabled": False}, {"key": "findings", "enabled": True}]
    assert section_fingerprint(hidden) != section_fingerprint(base)      # visibility matters


def test_save_preset_then_reject_duplicate(client, session_factory):
    order = [{"key": "cover", "enabled": True}, {"key": "summary", "enabled": True},
             {"key": "findings", "enabled": True}]
    r = client.post(f"{API}/report/section-presets", json={"name": "My layout", "order": order})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and body["preset"]["name"] == "My layout"
    with session_factory() as db:
        assert len(db.query(fm.ScribbleSectionPreset).all()) == 1
    # the identical arrangement (any name) is refused and names the existing preset
    dup = client.post(f"{API}/report/section-presets", json={"name": "Different name", "order": order})
    assert dup.status_code == 409
    assert "already saved" in dup.get_json()["detail"]
    with session_factory() as db:
        assert len(db.query(fm.ScribbleSectionPreset).all()) == 1  # no second row


def test_save_preset_requires_name_and_list(client):
    assert client.post(f"{API}/report/section-presets", json={"name": "", "order": []}).status_code == 400
    assert client.post(f"{API}/report/section-presets",
                       json={"name": "x", "order": "nope"}).status_code == 400


def test_delete_section_preset(client, session_factory):
    with session_factory() as db:
        p = fm.ScribbleSectionPreset(name="X", specs=[{"key": "cover", "enabled": True}],
                                     fingerprint=section_fingerprint([{"key": "cover", "enabled": True}]))
        db.add(p)
        db.commit()
        pid = p.id
    assert client.post(f"{UI}/report/section-presets/{pid}/delete").status_code == 302
    with session_factory() as db:
        assert db.get(fm.ScribbleSectionPreset, pid) is None


def test_composer_renders_preset_combobox_with_saved(client, session_factory):
    with session_factory() as db:
        db.add(fm.ScribbleSectionPreset(
            name="Client A layout", specs=[{"key": "cover", "enabled": True}],
            fingerprint=section_fingerprint([{"key": "cover", "enabled": True}])))
        db.commit()
    eng_id = _eng(session_factory)
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    assert "data-preset-select" in html          # the combobox, not the old buttons
    assert "data-save-preset" in html            # "Save this sequence"
    assert "Client A layout" in html             # the saved preset is offered
    assert "data-specs=" in html                 # options carry full order+visibility specs
    assert "Standard" in html and "Compliance-first" in html  # built-ins still listed
