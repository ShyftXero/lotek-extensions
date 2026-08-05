"""The HTTP surface: tenancy gating, the preview contract, branding, scope, and burn."""

from __future__ import annotations

import uuid

from cream.models import Document

_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


def test_health_reports_the_unit_vocabulary(client):
    body = client.get("/cream/api/health").get_json()
    assert body["status"] == "ok"
    assert "hr" in body["units"] and "project" in body["units"]


def test_create_requires_an_engagement(client):
    assert client.post("/cream/api/documents", json={"title": "x"}).status_code == 400
    assert client.post("/cream/api/documents",
                       json={"engagement_id": "not-a-uuid"}).status_code == 400


def test_create_applies_brand_defaults(client, engagement_id):
    client.put("/cream/api/brand", json={"default_currency": "GBP", "default_tax_label": "VAT 20%",
                                         "default_tax_pct": 20})
    doc = client.post("/cream/api/documents",
                      json={"engagement_id": str(engagement_id), "kind": "quote"}).get_json()
    assert doc["currency"] == "GBP"
    assert doc["tax_label"] == "VAT 20%"
    assert doc["tax_pct"] == 20.0
    assert doc["authorization_required"] is True  # quotes carry the authorization block by default


def test_save_replaces_line_items_and_recomputes(client, make_doc):
    doc = make_doc()
    saved = client.put(f"/cream/api/documents/{doc['id']}", json={
        "title": "Web app assessment",
        "tax_pct": 10,
        "line_items": [
            {"description": "Testing", "qty": 16, "unit": "hr", "unit_price": 250},
            {"description": "Reporting", "qty": 4, "unit": "hr", "unit_price": 200},
        ],
    }).get_json()
    assert saved["title"] == "Web app assessment"
    assert [li["unit"] for li in saved["line_items"]] == ["hr", "hr"]
    assert saved["totals"]["subtotal"] == 4800.0
    assert saved["totals"]["tax"] == 480.0
    assert saved["totals"]["total"] == 5280.0


def test_blank_rows_are_dropped_not_stored(client, make_doc):
    doc = make_doc()
    saved = client.put(f"/cream/api/documents/{doc['id']}", json={
        "line_items": [{"description": "Real", "qty": 1, "unit_price": 10},
                       {"description": "", "detail": "", "qty": 1, "unit_price": 0}],
    }).get_json()
    assert len(saved["line_items"]) == 1


def test_a_caller_cannot_set_status_or_number_through_save(client, make_doc):
    doc = make_doc()
    saved = client.put(f"/cream/api/documents/{doc['id']}",
                       json={"status": "issued", "number": "INV-2026-9999"}).get_json()
    assert saved["status"] == "draft"
    assert saved["number"] is None


def test_a_caller_cannot_move_a_document_to_another_engagement(client, make_doc, engagement_id):
    doc = make_doc()
    saved = client.put(f"/cream/api/documents/{doc['id']}",
                       json={"engagement_id": str(uuid.uuid7())}).get_json()
    assert saved["engagement_id"] == str(engagement_id)


# --- preview ---------------------------------------------------------------------------------------


def test_preview_renders_unsaved_state_without_persisting(client, make_doc, session_factory):
    doc = make_doc()
    out = client.post(f"/cream/api/documents/{doc['id']}/preview", json={
        "title": "Unsaved title",
        "line_items": [{"description": "Unsaved line", "qty": 2, "unit": "day",
                        "unit_price": 900}],
    }).get_json()
    assert "Unsaved line" in out["html"]
    assert out["totals"]["total"] == 1800.0
    assert out["frozen"] is False

    with session_factory() as db:
        stored = db.get(Document, uuid.UUID(doc["id"]))
        assert stored.title == "Test engagement"   # the savepoint rolled back
        assert stored.line_items == []


def test_preview_of_a_frozen_document_reports_frozen(client, make_doc):
    doc = make_doc()
    client.post(f"/cream/api/documents/{doc['id']}/issue")
    out = client.post(f"/cream/api/documents/{doc['id']}/preview", json={"title": "nope"}).get_json()
    assert out["frozen"] is True
    assert "nope" not in out["html"]


def test_saving_a_frozen_document_is_a_conflict(client, make_doc):
    doc = make_doc()
    client.post(f"/cream/api/documents/{doc['id']}/issue")
    assert client.put(f"/cream/api/documents/{doc['id']}",
                      json={"title": "nope"}).status_code == 409


# --- tenancy ---------------------------------------------------------------------------------------


def test_a_document_outside_the_visible_set_is_404_not_403(client, make_doc, hooks):
    doc = make_doc()
    hooks["visible_engagement_ids"] = frozenset()
    assert client.get(f"/cream/api/documents/{doc['id']}").status_code == 404
    assert client.get("/cream/api/documents").get_json()["documents"] == []


def test_write_is_refused_when_the_host_says_the_actor_cannot_operate(client, make_doc, hooks):
    doc = make_doc()
    hooks["can_operate_on"] = lambda eid: False
    assert client.put(f"/cream/api/documents/{doc['id']}", json={"title": "x"}).status_code == 403
    assert client.post(f"/cream/api/documents/{doc['id']}/issue").status_code == 403


def test_a_throwing_operator_hook_fails_closed(client, make_doc, hooks):
    doc = make_doc()

    def _boom(_eid):
        raise RuntimeError("host is down")

    hooks["can_operate_on"] = _boom
    assert client.post(f"/cream/api/documents/{doc['id']}/issue").status_code == 403


def test_read_only_actor_cannot_mutate(client, make_doc, hooks):
    doc = make_doc()
    hooks["can_write"] = False
    assert client.put(f"/cream/api/documents/{doc['id']}", json={"title": "x"}).status_code == 403
    assert client.put("/cream/api/brand", json={"company_name": "x"}).status_code == 403


# --- branding ----------------------------------------------------------------------------------------


def test_brand_round_trips_and_vets_the_logo(client):
    out = client.put("/cream/api/brand", json={
        "company_name": "Redteam Ltd", "logo_data_uri": _PNG, "accent_color": "#123456",
    }).get_json()
    assert out["company_name"] == "Redteam Ltd"
    assert out["logo_data_uri"] == _PNG

    rejected = client.put("/cream/api/brand",
                          json={"logo_data_uri": "https://evil.test/logo.png"}).get_json()
    assert rejected["logo_data_uri"] is None


def test_an_svg_logo_is_refused(client):
    out = client.put("/cream/api/brand", json={
        "logo_data_uri": "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
    }).get_json()
    assert out["logo_data_uri"] is None


def test_branding_is_admin_only(client, hooks):
    """`payment_instructions` says where the client sends money. A non-admin writer must not be able to
    re-route remittance on every future document."""
    from tests.conftest import FakeUser

    hooks["actor"] = FakeUser(role="operator")
    res = client.put("/cream/api/brand", json={"payment_instructions": "Wire to attacker-controlled"})
    assert res.status_code == 403

    # …and reading it is still fine — an operator needs to see the letterhead they are billing under.
    assert client.get("/cream/api/brand").status_code == 200


def test_an_operator_can_still_edit_documents(client, make_doc, hooks):
    """The branding gate must not bleed into ordinary document work."""
    from tests.conftest import FakeUser

    doc = make_doc()
    hooks["actor"] = FakeUser(role="operator")
    assert client.put(f"/cream/api/documents/{doc['id']}",
                      json={"title": "Still editable"}).status_code == 200


# --- scope, sync, burn -------------------------------------------------------------------------------


def test_scope_sync_takes_the_hosts_targets_not_the_bodys(client, make_doc, hooks):
    doc = make_doc()
    hooks["engagement_scope"] = lambda eid: ["10.0.0.0/24", "app.acme.test"]
    out = client.post(f"/cream/api/documents/{doc['id']}/scope-sync",
                      json={"scope": ["8.8.8.8"]}).get_json()
    assert out["scope"] == ["10.0.0.0/24", "app.acme.test"]
    assert "8.8.8.8" not in out["scope"]


def test_scope_sync_with_no_host_hook_is_empty_not_an_error(client, make_doc):
    doc = make_doc()
    out = client.post(f"/cream/api/documents/{doc['id']}/scope-sync").get_json()
    assert out == {"scope": [], "count": 0}


def test_sync_reads_units_from_the_host_when_the_body_omits_them(client, make_doc, hooks):
    doc = make_doc()
    hooks["engagement_units"] = lambda eid: ["run_type:web_app", "phase:retest"]
    out = client.post(f"/cream/api/documents/{doc['id']}/sync", json={}).get_json()
    by_key = {s["unit_key"]: s for s in out["suggestions"]}
    assert by_key["run_type:web_app"]["unit"] == "project"
    assert by_key["phase:retest"]["unit"] == "hr"        # hourly vs flat comes from the rate card


def test_sync_does_not_re_suggest_a_unit_already_billed(client, make_doc, hooks):
    doc = make_doc()
    client.put(f"/cream/api/documents/{doc['id']}", json={
        "line_items": [{"description": "Web app", "qty": 1, "unit_price": 4500,
                        "source": "run_type:web_app"}]})
    hooks["engagement_units"] = lambda eid: ["run_type:web_app"]
    out = client.post(f"/cream/api/documents/{doc['id']}/sync", json={}).get_json()
    assert out["suggestions"] == []


def test_burn_distinguishes_unmeasured_from_zero(client, make_doc, hooks):
    doc = make_doc()
    client.put(f"/cream/api/documents/{doc['id']}", json={
        "line_items": [
            {"description": "Testing", "qty": 16, "unit": "hr", "unit_price": 250,
             "source": "run_type:web_app"},
            {"description": "Reporting", "qty": 4, "unit": "hr", "unit_price": 200,
             "source": "phase:reporting"},
        ]})
    hooks["engagement_burn"] = lambda eid: {"run_type:web_app": 11.5}
    out = client.get(f"/cream/api/documents/{doc['id']}/burn").get_json()
    assert out["available"] is True
    rows = {r["source"]: r for r in out["rows"]}
    assert rows["run_type:web_app"]["executed_qty"] == 11.5
    assert rows["run_type:web_app"]["delta"] == -4.5
    assert rows["phase:reporting"]["executed_qty"] is None   # not measured, NOT zero
    assert rows["phase:reporting"]["delta"] is None


def test_burn_without_a_host_hook_reports_unavailable(client, make_doc):
    doc = make_doc()
    assert client.get(f"/cream/api/documents/{doc['id']}/burn").get_json()["available"] is False


# --- lifecycle over HTTP -----------------------------------------------------------------------------


def test_quote_to_invoice_over_http(client, engagement_id):
    quote = client.post("/cream/api/documents",
                        json={"engagement_id": str(engagement_id), "kind": "quote"}).get_json()
    client.put(f"/cream/api/documents/{quote['id']}", json={
        "line_items": [{"description": "External test", "qty": 1, "unit_price": 5000}]})
    client.post(f"/cream/api/documents/{quote['id']}/issue")
    client.post(f"/cream/api/documents/{quote['id']}/accept")

    res = client.post(f"/cream/api/documents/{quote['id']}/convert")
    assert res.status_code == 201
    invoice = res.get_json()
    assert invoice["kind"] == "invoice"
    assert invoice["status"] == "draft"
    assert invoice["converted_from_id"] == quote["id"]
    assert invoice["totals"]["total"] == 5000.0


def test_converting_an_unissued_quote_is_a_conflict(client, engagement_id):
    quote = client.post("/cream/api/documents",
                        json={"engagement_id": str(engagement_id), "kind": "quote"}).get_json()
    assert client.post(f"/cream/api/documents/{quote['id']}/convert").status_code == 409
