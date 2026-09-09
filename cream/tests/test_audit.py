"""Per-entity audit on business state changes (lotek#585 CLASS 7 / INV-AUDIT-03).

Every lifecycle transition and every brand edit must leave EXACTLY ONE ``ext:cream:<verb>`` audit row, in
the same transaction as the change, naming the entity — and, for a transition or a brand edit, carrying
before/after. The coarse ``EXTENSION_MACHINE_WRITE`` backstop core already fires records only THAT a write
happened; this is the richer per-entity trail (``payment_instructions`` remittance-redirect visibility
above all).
"""

from __future__ import annotations


def _rows(app, action):
    return [r for r in app.audit_log if r["action"] == action]


def test_issue_emits_one_audit_row_with_status_before_after(app, client, make_doc):
    doc = make_doc()
    assert app.audit_log == []  # create itself is covered by the coarse backstop, not per-entity here
    client.post(f"/cream/api/documents/{doc['id']}/issue")
    rows = _rows(app, "ext:cream:issue")
    assert len(rows) == 1
    r = rows[0]
    assert r["subject_type"] == "cream_document"
    assert r["subject_id"] == doc["id"]  # coerced to the string the API response carries
    assert r["before"]["status"] == "draft"
    assert r["after"]["status"] == "issued"
    assert r["after"]["number"]  # a number was assigned at issue


def test_void_emits_one_audit_row(app, client, make_doc):
    doc = make_doc()
    client.post(f"/cream/api/documents/{doc['id']}/void")
    rows = _rows(app, "ext:cream:void")
    assert len(rows) == 1
    assert rows[0]["after"]["status"] == "void"


def test_mark_sent_emits_one_audit_row(app, client, make_doc):
    doc = make_doc()
    client.post(f"/cream/api/documents/{doc['id']}/issue")
    app.audit_log.clear()
    client.post(f"/cream/api/documents/{doc['id']}/mark-sent")
    rows = _rows(app, "ext:cream:mark_sent")
    assert len(rows) == 1
    assert rows[0]["before"]["status"] == "issued"
    assert rows[0]["after"]["status"] == "sent"


def test_accept_and_convert_each_emit_one_audit_row(app, client, make_doc):
    quote = make_doc(kind="quote")
    client.post(f"/cream/api/documents/{quote['id']}/issue")
    app.audit_log.clear()
    assert client.post(f"/cream/api/documents/{quote['id']}/accept").status_code == 200
    assert len(_rows(app, "ext:cream:accept")) == 1
    assert _rows(app, "ext:cream:accept")[0]["after"]["status"] == "accepted"

    res = client.post(f"/cream/api/documents/{quote['id']}/convert")
    assert res.status_code == 201
    conv = _rows(app, "ext:cream:convert")
    assert len(conv) == 1
    # The subject is the NEW invoice, and before names the source quote.
    assert conv[0]["subject_id"] == res.get_json()["id"]
    assert conv[0]["before"]["converted_from"] == quote["id"]


def test_write_brand_audits_before_and_after_payment_instructions(app, client):
    client.put("/cream/api/brand", json={"payment_instructions": "Wire to Acme Bank acct 111"})
    app.audit_log.clear()
    client.put("/cream/api/brand", json={"payment_instructions": "Wire to ATTACKER Bank acct 999"})
    rows = _rows(app, "ext:cream:update_brand")
    assert len(rows) == 1
    r = rows[0]
    assert r["subject_type"] == "cream_brand"
    assert r["before"]["payment_instructions"] == "Wire to Acme Bank acct 111"
    assert r["after"]["payment_instructions"] == "Wire to ATTACKER Bank acct 999"


def test_a_failed_transition_leaves_no_audit_row(app, client, make_doc):
    """The audit call is IN-BAND, before commit — a rejected transition (issuing an already-issued doc,
    409) rolls back and leaves no row. Proven: audit fires only on the successful path."""
    doc = make_doc()
    client.post(f"/cream/api/documents/{doc['id']}/issue")
    app.audit_log.clear()
    assert client.post(f"/cream/api/documents/{doc['id']}/issue").status_code == 409
    assert _rows(app, "ext:cream:issue") == []


def test_manifest_declares_every_emitted_audit_verb():
    """INV-AUDIT-03 reader half: every verb passed to host_audit in api.py must be declared in the
    manifest's [audit] verbs, or the host's /admin/audit filter can't select that trail. This greps the
    real call sites, so adding a host_audit verb without declaring it turns this red."""
    import pathlib
    import re
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[1]
    manifest = tomllib.loads((root / "lotek-extension.toml").read_text())
    declared = set(manifest["audit"]["verbs"])

    api_src = (root / "cream" / "api.py").read_text()
    emitted = set(re.findall(r'host_audit\(\s*db,\s*"([a-z_]+)"', api_src))
    emitted |= set(re.findall(r'_lifecycle_audit\(\s*db,\s*doc,\s*"([a-z_]+)"', api_src))

    assert emitted, "no audit verbs found in api.py — the drift-guard regex is stale"
    assert emitted <= declared, f"emitted but undeclared in [audit] verbs: {sorted(emitted - declared)}"
