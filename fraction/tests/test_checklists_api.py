"""Engagement-checklist JSON API (browser surface). Drives the endpoints the UI calls: library CRUD +
import/export/hide/reset, assignment, and per-item updates."""

from __future__ import annotations

import re

from fraction.models import ChecklistTemplate, Engagement, EngagementFinding

API = "/fraction/api"


def _mk_engagement(session_factory) -> int:
    with session_factory() as db:
        e = Engagement(name="API Eng")
        db.add(e)
        db.commit()
        return e.id


def _template_id(session_factory, slug: str) -> int:
    with session_factory() as db:
        return db.query(ChecklistTemplate).filter_by(slug=slug).one().id


def test_list_templates_hides_hidden_by_default(client, session_factory):
    resp = client.get(f"{API}/checklists/templates")
    data = resp.get_json()
    assert data["ok"] and len(data["templates"]) == 7
    # hide one, then it drops out of the default list but shows with ?hidden=1
    tid = _template_id(session_factory, "owasp-wstg")
    client.post(f"{API}/checklists/templates/{tid}/hide", json={"hidden": True})
    assert len(client.get(f"{API}/checklists/templates").get_json()["templates"]) == 6
    assert len(client.get(f"{API}/checklists/templates?hidden=1").get_json()["templates"]) == 7


def test_suggest_groups_by_category(client):
    data = client.get(f"{API}/checklists/templates/suggest?category=web-app").get_json()
    assert data["ok"]
    slugs = {t["slug"] for t in data["suggested"]}
    assert "web-app-api" in slugs and "ai-llm-security" in slugs
    # a reminder in a different category is NOT suggested for web-app
    assert "global-pre-engagement" not in slugs


def test_import_markdown_creates_template(client):
    md = "# Imported\n\n## Sec\n- [ ] **Item A**: guidance a\n- [ ] **Item B**: guidance b\n"
    resp = client.post(f"{API}/checklists/templates", json={"markdown": md, "kind": "coverage"})
    assert resp.status_code == 201
    t = resp.get_json()["template"]
    assert t["name"] == "Imported" and t["item_count"] == 2 and t["builtin"] is False


def test_create_rejects_empty(client):
    resp = client.post(f"{API}/checklists/templates", json={"markdown": "# Empty\n"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_edit_marks_builtin_customized(client, session_factory):
    tid = _template_id(session_factory, "ai-llm-security")
    resp = client.post(f"{API}/checklists/templates/{tid}", json={"name": "AI Custom"})
    t = resp.get_json()["template"]
    assert t["name"] == "AI Custom" and t["customized"] is True


def test_reset_restores_builtin(client, session_factory):
    tid = _template_id(session_factory, "pci-dss-segmentation")
    client.post(f"{API}/checklists/templates/{tid}", json={"name": "Mangled"})
    resp = client.post(f"{API}/checklists/templates/{tid}/reset")
    t = resp.get_json()["template"]
    assert t["name"] == "PCI-DSS Segmentation Testing" and t["customized"] is False


def test_reset_rejected_for_non_builtin(client):
    md = "# Mine\n- [ ] **x**: y\n"
    tid = client.post(f"{API}/checklists/templates", json={"markdown": md}).get_json()["template"]["id"]
    assert client.post(f"{API}/checklists/templates/{tid}/reset").status_code == 400


def test_duplicate_forks_a_copy(client, session_factory):
    tid = _template_id(session_factory, "web-app-api")
    resp = client.post(f"{API}/checklists/templates/{tid}/duplicate")
    t = resp.get_json()["template"]
    assert t["name"].endswith("(copy)") and t["builtin"] is False


def test_export_json_and_markdown(client, session_factory):
    tid = _template_id(session_factory, "owasp-asvs-l1")
    j = client.get(f"{API}/checklists/templates/{tid}/export?format=json")
    assert j.mimetype == "application/json" and b"control_ref" in j.data
    m = client.get(f"{API}/checklists/templates/{tid}/export?format=md")
    assert m.mimetype == "text/markdown" and m.data.startswith(b"# OWASP ASVS L1")


def test_assign_list_and_rollup(client, session_factory):
    eid = _mk_engagement(session_factory)
    tid = _template_id(session_factory, "pci-dss-segmentation")
    # zero to start
    assert client.get(f"{API}/engagements/{eid}/checklists").get_json()["checklists"] == []
    resp = client.post(f"{API}/engagements/{eid}/checklists", json={"template_id": tid})
    assert resp.status_code == 201
    ec = resp.get_json()["checklist"]
    assert ec["kind"] == "compliance" and ec["include_in_report"] is True
    assert ec["rollup"]["open"] == len(ec["items"])
    assert "pass" in ec["recommended_status"]
    # update an item -> rollup shifts
    item_id = ec["items"][0]["id"]
    up = client.post(f"{API}/engagement-checklist-items/{item_id}", json={"status": "pass", "note": "done"})
    assert up.get_json()["item"]["bucket"] == "satisfied"
    listed = client.get(f"{API}/engagements/{eid}/checklists").get_json()["checklists"][0]
    assert listed["rollup"]["satisfied"] == 1


def test_import_sanitizes_slug(client):
    # A hostile/dirty slug on import must be sanitized (W1): no spaces, quotes, CRLF, or path chars.
    payload = {"template": {"name": "X", "items": [{"text": "a"}], "slug": 'e"\r\nSet-Cookie: x/../../etc'}}
    t = client.post(f"{API}/checklists/templates", json=payload).get_json()["template"]
    assert re.fullmatch(r"[a-z0-9-]+", t["slug"]), t["slug"]
    assert len(t["slug"]) <= 128


def test_finding_link_must_be_same_engagement(client, session_factory):
    # W4: finding_id must reference a finding in THIS engagement; a cross-engagement link is rejected.
    with session_factory() as db:
        e1, e2 = Engagement(name="E1"), Engagement(name="E2")
        db.add_all([e1, e2])
        db.flush()
        f_other = EngagementFinding(engagement_id=e2.id, title="foreign finding")
        f_same = EngagementFinding(engagement_id=e1.id, title="local finding")
        db.add_all([f_other, f_same])
        db.commit()
        e1_id, foreign_fid, local_fid = e1.id, f_other.id, f_same.id
    tid = _template_id(session_factory, "web-app-api")
    resp = client.post(f"{API}/engagements/{e1_id}/checklists", json={"template_id": tid})
    ec = resp.get_json()["checklist"]
    iid = ec["items"][0]["id"]
    # cross-engagement finding -> 400
    bad = client.post(f"{API}/engagement-checklist-items/{iid}", json={"finding_id": foreign_fid})
    assert bad.status_code == 400 and bad.get_json()["ok"] is False
    # same-engagement finding -> accepted
    good = client.post(f"{API}/engagement-checklist-items/{iid}", json={"finding_id": local_fid})
    assert good.get_json()["item"]["finding_id"] == local_fid
    # unlink -> accepted
    unl = client.post(f"{API}/engagement-checklist-items/{iid}", json={"finding_id": None})
    assert unl.get_json()["item"]["finding_id"] is None


def test_assign_rejects_non_integer_template_id(client, session_factory):
    eid = _mk_engagement(session_factory)
    resp = client.post(f"{API}/engagements/{eid}/checklists", json={"template_id": "abc"})
    assert resp.status_code == 400


def test_custom_status_buckets_open(client, session_factory):
    eid = _mk_engagement(session_factory)
    tid = _template_id(session_factory, "pci-dss-segmentation")
    ec = client.post(f"{API}/engagements/{eid}/checklists", json={"template_id": tid}).get_json()["checklist"]
    iid = ec["items"][0]["id"]
    out = client.post(f"{API}/engagement-checklist-items/{iid}", json={"status": "Compensating Control"})
    assert out.get_json()["item"]["status"] == "Compensating Control"
    assert out.get_json()["item"]["bucket"] == "open"


def test_toggle_include_in_report_and_unassign(client, session_factory):
    eid = _mk_engagement(session_factory)
    tid = _template_id(session_factory, "global-pre-engagement")
    ec = client.post(f"{API}/engagements/{eid}/checklists", json={"template_id": tid}).get_json()["checklist"]
    assert ec["include_in_report"] is False  # reminder default
    toggled = client.post(f"{API}/engagement-checklists/{ec['id']}", json={"include_in_report": True})
    assert toggled.get_json()["checklist"]["include_in_report"] is True
    assert client.post(f"{API}/engagement-checklists/{ec['id']}/delete").get_json()["ok"] is True
    assert client.get(f"{API}/engagements/{eid}/checklists").get_json()["checklists"] == []
