"""The human surface — the pages exist, gate on the same rules as the API, and export."""

from __future__ import annotations

import re


def _identity_cell(body: str, doc_id: str) -> str:
    """The rendered text of the list row's identity cell, i.e. the anchor whose href is exactly this
    document's view URL.

    Asserted at the HTML level on purpose: the defect in ext#46 was not in a helper's return value, it
    was what the row *said* to the human reading it. A test that only checked ``document_handle`` would
    have stayed green through a template that never used it — and the row is the surface the client saw.
    The closing quote after the id keeps this from matching the row's Edit/PDF links.
    """
    match = re.search(rf'<a[^>]*href="[^"]*/documents/{re.escape(doc_id)}"[^>]*>(.*?)</a>', body, re.S)
    assert match, f"no view link for document {doc_id} in the list"
    return " ".join(match.group(1).split())


def _view_heading(body: str) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    assert match, "no <h1> on the document view page"
    return " ".join(match.group(1).split())


def test_a_draft_row_names_itself_with_a_tail_truncated_handle(client, make_doc):
    """ext#46: an unissued document's cell used to be a bare ``—`` — and since that cell IS the row's
    link, a one-character click target with no identity in it."""
    doc = make_doc(title="Unissued work")
    body = client.get("/cream/").get_data(as_text=True)
    assert _identity_cell(body, doc["id"]) == f"draft …{doc['id'][-10:]}"


def test_the_draft_row_carries_the_whole_id_for_copying(client, make_doc):
    """A truncated handle must not be a dead end — the full id is on the link itself."""
    doc = make_doc()
    body = client.get("/cream/").get_data(as_text=True)
    assert f'title="{doc["id"]}"' in body


def test_an_issued_row_shows_its_frozen_number_and_no_handle(client, make_doc):
    """The handle is a stand-in, not an addition: once a number exists it is the identity."""
    doc = make_doc()
    number = client.post(f"/cream/api/documents/{doc['id']}/issue").get_json()["number"]
    assert number
    cell = _identity_cell(client.get("/cream/").get_data(as_text=True), doc["id"])
    assert cell == number
    assert "…" not in cell


def test_a_voided_draft_is_not_labelled_a_draft(client, make_doc):
    """``void`` accepts a draft, so an unnumbered document is not necessarily a draft — the handle's
    leading word is the document's status, and must track it."""
    doc = make_doc()
    assert client.post(f"/cream/api/documents/{doc['id']}/void").status_code == 200
    cell = _identity_cell(client.get("/cream/").get_data(as_text=True), doc["id"])
    assert cell == f"void …{doc['id'][-10:]}"


def test_the_view_page_heading_names_an_unissued_document(client, make_doc):
    """``Invoice (draft)`` was the same heading on every draft. The handle makes the page self-identify —
    which is what a screenshot or a PDF print of it needs."""
    doc = make_doc()
    heading = _view_heading(client.get(f"/cream/documents/{doc['id']}").get_data(as_text=True))
    assert heading == f"Invoice draft …{doc['id'][-10:]}"


def test_the_view_page_heading_of_an_issued_document_is_its_number(client, make_doc):
    doc = make_doc()
    number = client.post(f"/cream/api/documents/{doc['id']}/issue").get_json()["number"]
    heading = _view_heading(client.get(f"/cream/documents/{doc['id']}").get_data(as_text=True))
    assert heading == f"Invoice {number}"


def test_dashboard_lists_a_document(client, make_doc):
    make_doc(title="External assessment")
    body = client.get("/cream/").get_data(as_text=True)
    assert "External assessment" in body
    assert "New document" in body


def test_editor_serves_the_form_and_a_server_rendered_preview(client, make_doc):
    doc = make_doc()
    body = client.get(f"/cream/documents/{doc['id']}/edit").get_data(as_text=True)
    assert 'data-f="title"' in body
    assert 'id="preview"' in body
    assert "cream-doc" in body          # the preview pane starts populated, not blank
    assert "/preview" in body           # and the debounce posts back to the server renderer


def test_the_editor_redirects_a_frozen_document_to_the_read_view(client, make_doc):
    doc = make_doc()
    client.post(f"/cream/api/documents/{doc['id']}/issue")
    body = client.get(f"/cream/documents/{doc['id']}/edit").get_data(as_text=True)
    assert 'data-f="title"' not in body     # no dead inputs
    assert "frozen at issue" in body


def test_a_read_only_actor_gets_no_edit_affordances(client, make_doc, hooks):
    make_doc()
    hooks["can_write"] = False
    body = client.get("/cream/").get_data(as_text=True)
    assert "New document" not in body


def test_view_page_offers_both_exports(client, make_doc):
    doc = make_doc()
    body = client.get(f"/cream/documents/{doc['id']}").get_data(as_text=True)
    assert "export.html" in body
    assert "export.pdf" in body


def test_html_export_is_a_standalone_attachment(client, make_doc):
    doc = make_doc()
    res = client.get(f"/cream/documents/{doc['id']}/export.html")
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"]
    assert res.get_data(as_text=True).startswith("<!doctype html>")


def test_pdf_export_is_a_pdf_or_an_honest_503(client, make_doc):
    """weasyprint is optional. Either it renders a real PDF, or the route says so plainly — what it
    must never do is hand back HTML under a .pdf filename."""
    doc = make_doc()
    res = client.get(f"/cream/documents/{doc['id']}/export.pdf")
    if res.status_code == 200:
        assert res.mimetype == "application/pdf"
        assert res.get_data()[:5] == b"%PDF-"
    else:
        assert res.status_code == 503
        assert "weasyprint" in res.get_data(as_text=True)


def test_branding_page_renders_the_singleton(client):
    body = client.get("/cream/brand").get_data(as_text=True)
    assert 'data-b="company_name"' in body
    assert "Remove logo" in body


def test_a_document_outside_the_visible_set_is_404_in_the_ui_too(client, make_doc, hooks):
    doc = make_doc()
    hooks["visible_engagement_ids"] = frozenset()
    assert client.get(f"/cream/documents/{doc['id']}").status_code == 404
    assert client.get(f"/cream/documents/{doc['id']}/edit").status_code == 404
    assert client.get(f"/cream/documents/{doc['id']}/export.pdf").status_code == 404


def test_new_document_form_asks_for_the_engagement(client):
    body = client.get("/cream/documents/new").get_data(as_text=True)
    assert 'id="f-engagement"' in body


def test_every_page_ships_the_csrf_plumbing(client, make_doc):
    """Mounted in lotek every mutating call is CSRF-protected. If the editor stopped sending the header
    the only symptom would be a 400 on save, so pin that the plumbing is on the page."""
    doc = make_doc()
    for path in ("/cream/", "/cream/brand", "/cream/documents/new",
                 f"/cream/documents/{doc['id']}/edit"):
        body = client.get(path).get_data(as_text=True)
        assert "window.CREAM_CSRF" in body, path
        assert "X-CSRFToken" in body, path
