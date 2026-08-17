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


def _tab_title(body: str) -> str:
    """The browser-tab title. When two pages are open on two documents this is the ONLY identity the
    reader can see of the one that is behind — which is why it is asserted as its own surface."""
    match = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
    assert match, "no <title> on the page"
    return " ".join(match.group(1).split())


def _attachment_filename(res) -> str:
    disposition = res.headers["Content-Disposition"]
    match = re.search(r'filename="([^"]*)"', disposition)
    assert match, f"no filename in {disposition!r}"
    return match.group(1)


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
    which is what a screenshot of the IN-APP page needs.

    Scope note, because the first version of this docstring got it wrong: this ``<h1>`` is cream's page
    chrome and never reaches an export or a print. ``export.html``/``export.pdf`` render the document
    itself through ``render.py``, which emits its own heading — their identity is pinned separately by
    ``test_a_draft_export_names_itself_in_its_filename_and_tab_title``.
    """
    doc = make_doc()
    heading = _view_heading(client.get(f"/cream/documents/{doc['id']}").get_data(as_text=True))
    assert heading == f"Invoice draft …{doc['id'][-10:]}"


def test_the_editor_heading_names_the_draft_it_is_editing(client, make_doc):
    """The editor is where a draft is actually worked — a draft row's only action link is ``Edit``, not
    ``View`` (``list.html``) — and it was headed ``Invoice draft`` for every draft alike, the same defect
    the view page was fixed for.
    """
    first, second = make_doc(title="One"), make_doc(title="Two")
    headings = [
        _view_heading(client.get(f"/cream/documents/{doc['id']}/edit").get_data(as_text=True))
        for doc in (first, second)
    ]
    assert headings == [f"Invoice draft …{first['id'][-10:]}",
                        f"Invoice draft …{second['id'][-10:]}"]
    assert headings[0] != headings[1]


def test_two_open_drafts_have_four_distinguishable_browser_tabs(client, make_doc):
    """Both single-document pages, both drafts: four tabs that read ``CREAM — Document`` /
    ``CREAM — Edit document`` are four tabs a reader cannot tell apart, which is the ext#46 complaint one
    surface further in than the list."""
    first, second = make_doc(), make_doc()
    titles = []
    for doc in (first, second):
        for path in (f"/cream/documents/{doc['id']}", f"/cream/documents/{doc['id']}/edit"):
            title = _tab_title(client.get(path).get_data(as_text=True))
            assert f"…{doc['id'][-10:]}" in title, (path, title)
            titles.append(title)
    assert len(set(titles)) == 4, titles


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


def test_a_draft_export_names_itself_in_its_filename_and_tab_title(client, make_doc):
    """An unissued export was ``document.html`` titled a bare ``Invoice``, for every draft — so three
    downloads landed as ``document.pdf``, ``document(1).pdf``, ``document(2).pdf``.

    The filename is ASCII on purpose: a non-ASCII ``filename=`` needs RFC 5987's ``filename*`` form to
    survive every browser, so the stem drops the display handle's ``…`` and space.
    """
    doc = make_doc()
    tail = doc["id"][-10:]
    res = client.get(f"/cream/documents/{doc['id']}/export.html")
    assert res.status_code == 200
    name = _attachment_filename(res)
    assert name == f"invoice-draft-{tail}.html"
    assert name.isascii()
    assert _tab_title(res.get_data(as_text=True)) == f"Invoice draft …{tail}"


def test_an_issued_export_is_still_named_by_its_number(client, make_doc):
    """The handle is a stand-in, here too: once a number exists the file and the title are the number,
    exactly as before this branch."""
    doc = make_doc()
    number = client.post(f"/cream/api/documents/{doc['id']}/issue").get_json()["number"]
    res = client.get(f"/cream/documents/{doc['id']}/export.html")
    assert _attachment_filename(res) == f"{number}.html"
    assert _tab_title(res.get_data(as_text=True)) == f"Invoice {number}"


def test_the_exported_document_body_does_not_print_an_id_as_a_number(client, make_doc):
    """Deliberate boundary: the filename and the tab name a draft, the DOCUMENT does not. The body is the
    client's copy, and an id tail sitting where an invoice number goes reads as an invoice number. A draft
    gets its printed identity at issue."""
    doc = make_doc()
    body = client.get(f"/cream/documents/{doc['id']}/export.html").get_data(as_text=True)
    document = body.split("</head>", 1)[1]
    assert doc["id"][-10:] not in document
    assert 'class="num"' not in document


def test_pdf_export_is_a_pdf_or_an_honest_503(client, make_doc):
    """weasyprint is optional. Either it renders a real PDF, or the route says so plainly — what it
    must never do is hand back HTML under a .pdf filename.

    Which branch runs depends on the environment: weasyprint is in cream's ``pdf`` extra, NOT ``dev``, so
    a plain ``uv run --extra dev pytest`` takes the **503** branch and proves nothing about PDF rendering.
    ``uv run --extra dev --extra pdf pytest`` takes the 200 branch. Read the branch, not the green dot.
    """
    doc = make_doc()
    res = client.get(f"/cream/documents/{doc['id']}/export.pdf")
    if res.status_code == 200:
        assert res.mimetype == "application/pdf"
        assert res.get_data()[:5] == b"%PDF-"
        assert _attachment_filename(res) == f"invoice-draft-{doc['id'][-10:]}.pdf"
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
