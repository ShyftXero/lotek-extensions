"""ext#141/#142/#143 — three built-but-unreachable scribble surfaces, now reachable from the browser.

- #143 batch move: a cookie sibling of the machine bulk-move, for the board's multi-select.
- #141 attack-path linking: cookie link/unlink (the picker in board.js talks to vector directly).
- #142 vuln-map: a library page to list/add/delete the title/source -> template mappings.

Each backend already existed (machine API); the defect was no template/route reached it. These assert
the reaching, plus the tenancy/atomicity the machine path already had.
"""
from __future__ import annotations

import uuid

from scribble.enums import Severity
from scribble.models import (
    Client,
    Engagement,
    EngagementDiagram,
    EngagementFinding,
    FindingGroup,
    ScribbleVulnMap,
    VulnerabilityTemplate,
)

API = "/scribble/api"
UI = "/scribble"


def _make_template(db, name="Weak SMB Signing", severity=Severity.low):
    from scribble.content import schema
    t = VulnerabilityTemplate(name=name, category="Test", default_severity=severity,
                              content_json={"description": schema.doc_from_text(f"{name}.")})
    db.add(t)
    db.commit()
    return t


def _make_engagement(db, name="Q3"):
    c = Client(name=f"{name} Client")
    db.add(c)
    db.flush()
    eng = Engagement(name=name, client_id=c.id, company_name=f"{name} Corp")
    db.add(eng)
    db.commit()
    return eng


def _make_group(db, eng, name, order_index=0):
    g = FindingGroup(engagement=eng, name=name, order_index=order_index)
    db.add(g)
    db.commit()
    return g


# ── #143: batch move ─────────────────────────────────────────────────────────────────────────────────

def test_batch_move_moves_several_findings_into_a_group(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        a = _make_group(db, eng, "A", 0)
        b = _make_group(db, eng, "B", 1)
        t = _make_template(db)
        fs = [EngagementFinding.from_template(t, engagement_id=eng.id, group_id=a.id, order_index=i)
              for i in range(3)]
        for f in fs:
            db.add(f)
        db.commit()
        eng_id, b_id = eng.id, b.id
        ids = [str(fs[0].id), str(fs[1].id)]

    resp = client.post(f"{API}/engagements/{eng_id}/findings/move",
                       json={"finding_ids": ids, "group_id": str(b_id), "order_index": 0})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with session_factory() as db:
        for fid in ids:
            assert db.get(EngagementFinding, uuid.UUID(fid)).group_id == b_id


def test_batch_move_is_atomic_a_foreign_id_moves_nothing(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        a = _make_group(db, eng, "A", 0)
        b = _make_group(db, eng, "B", 1)
        t = _make_template(db)
        f = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=a.id, order_index=0)
        db.add(f)
        db.commit()
        eng_id, b_id, a_id, f_id = eng.id, b.id, a.id, f.id
        foreign = str(uuid.uuid7())

    resp = client.post(f"{API}/engagements/{eng_id}/findings/move",
                       json={"finding_ids": [str(f_id), foreign], "group_id": str(b_id), "order_index": 0})
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.get(EngagementFinding, f_id).group_id == a_id, "nothing moves when any id is foreign"


def test_batch_move_group_null_goes_ungrouped(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        a = _make_group(db, eng, "A", 0)
        t = _make_template(db)
        f = EngagementFinding.from_template(t, engagement_id=eng.id, group_id=a.id, order_index=0)
        db.add(f)
        db.commit()
        eng_id, f_id = eng.id, f.id

    resp = client.post(f"{API}/engagements/{eng_id}/findings/move",
                       json={"finding_ids": [str(f_id)], "group_id": None, "order_index": 0})
    assert resp.status_code == 200
    with session_factory() as db:
        assert db.get(EngagementFinding, f_id).group_id is None


def test_board_renders_multiselect_checkboxes(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        a = _make_group(db, eng, "A", 0)
        t = _make_template(db)
        db.add(EngagementFinding.from_template(t, engagement_id=eng.id, group_id=a.id, order_index=0))
        db.commit()
        eng_id = eng.id
    body = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    assert "scribble-finding-check" in body
    assert "scribble-bulk-bar" in body
    assert f"{API}/engagements/{eng_id}/findings/move" in body


# ── #141: attack-path link / unlink ────────────────────────────────────────────────────────────────

def test_link_attack_path_creates_a_diagram(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        eng_id = eng.id
    resp = client.post(f"{API}/engagements/{eng_id}/attack-paths",
                       json={"embed_html": "<p>diagram</p>", "diagram_ref": "vec-1", "caption": "Path A"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with session_factory() as db:
        rows = list(db.get(Engagement, eng_id).diagrams)
        assert len(rows) == 1 and rows[0].caption == "Path A" and rows[0].embed_html == "<p>diagram</p>"


def test_link_attack_path_requires_embed_html(client, session_factory):
    with session_factory() as db:
        eng_id = _make_engagement(db).id
    resp = client.post(f"{API}/engagements/{eng_id}/attack-paths", json={"caption": "no html"})
    assert resp.status_code == 400


def test_unlink_attack_path_removes_it(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        d = EngagementDiagram(engagement_id=eng.id, embed_html="<p>x</p>", caption="c", order_index=0)
        db.add(d)
        db.commit()
        eng_id, d_id = eng.id, d.id
    resp = client.post(f"{UI}/engagements/{eng_id}/attack-paths/{d_id}/unlink")
    assert resp.status_code in (302, 303)
    with session_factory() as db:
        assert db.get(EngagementDiagram, d_id) is None


def test_unlink_attack_path_404s_across_engagements(client, session_factory):
    with session_factory() as db:
        eng_a = _make_engagement(db, "A")
        eng_b = _make_engagement(db, "B")
        d = EngagementDiagram(engagement_id=eng_a.id, embed_html="<p>x</p>", order_index=0)
        db.add(d)
        db.commit()
        eng_b_id, d_id = eng_b.id, d.id
    # deleting engagement A's diagram via engagement B's URL must 404 and delete nothing
    resp = client.post(f"{UI}/engagements/{eng_b_id}/attack-paths/{d_id}/unlink")
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.get(EngagementDiagram, d_id) is not None


def test_board_renders_attack_path_section(client, session_factory):
    with session_factory() as db:
        eng_id = _make_engagement(db).id
    body = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    assert "Attack paths" in body
    assert "scribble-diagram-select" in body
    assert f"{API}/engagements/{eng_id}/attack-paths" in body


# ── #142: vuln-map ───────────────────────────────────────────────────────────────────────────────────

def test_vuln_map_page_lists_and_add_form(client, session_factory):
    with session_factory() as db:
        _make_template(db, "TLS misconfig")
    body = client.get(f"{UI}/library/vuln-map").get_data(as_text=True)
    assert "Vuln-map" in body
    assert 'name="template_id"' in body


def test_vuln_map_create_and_delete(client, session_factory, clean_vuln_map):
    with session_factory() as db:
        t_id = _make_template(db, "TLS misconfig").id
    resp = client.post(f"{UI}/library/vuln-map",
                       data={"source": "nuclei", "template_id": str(t_id)})
    assert resp.status_code in (302, 303)
    with session_factory() as db:
        rows = db.query(ScribbleVulnMap).all()
        assert len(rows) == 1 and rows[0].source == "nuclei" and rows[0].template_id == t_id
        map_id = rows[0].id
    resp = client.post(f"{UI}/library/vuln-map/{map_id}/delete")
    assert resp.status_code in (302, 303)
    with session_factory() as db:
        assert db.query(ScribbleVulnMap).count() == 0


def test_vuln_map_create_requires_a_template(client, session_factory, clean_vuln_map):
    client.post(f"{UI}/library/vuln-map", data={"source": "nuclei"})  # no template_id
    with session_factory() as db:
        assert db.query(ScribbleVulnMap).count() == 0


def test_vuln_map_create_requires_a_match_key(client, session_factory, clean_vuln_map):
    with session_factory() as db:
        t_id = _make_template(db).id
    client.post(f"{UI}/library/vuln-map", data={"template_id": str(t_id)})  # no source/title/dedupe
    with session_factory() as db:
        assert db.query(ScribbleVulnMap).count() == 0
