"""The list page after adopting the kit reporting editor: the create + edit forms mount the editor and
the read view renders the stored body as sanitized HTML instead of a raw ``<pre>`` dump.

These are Flask-client render checks (no browser), so they pin the MARKUP contract the editor's own JS
depends on (the container, the embedded doc, the hidden serialized field, the /_kit assets) and the
server-side render/escape — not the in-browser editing itself, which lands in a mounted Playwright run.
"""

from __future__ import annotations


def test_create_and_edit_forms_mount_the_kit_editor(client):
    client.post("/bugreport/", data={"title": "t", "body": "hello world"})
    html = client.get("/bugreport/").get_data(as_text=True)
    # create form + the report's (collapsed) edit form each carry an editor container + hidden body
    assert html.count("data-br-editor") >= 2
    assert 'data-api-base="/bugreport/' in html   # the edit form's report-scoped api base
    assert "data-br-body" in html                 # the hidden field the submit handler serializes into
    assert "reporting-editor.js" in html and "reporting-outbox.js" in html  # /_kit assets load


def test_read_view_renders_sanitized_html_not_a_raw_pre(client):
    client.post("/bugreport/", data={"title": "t", "body": "plain <script>x</script>"})
    html = client.get("/bugreport/").get_data(as_text=True)
    assert 'class="br-body"' in html
    assert "&lt;script&gt;" in html                    # escaped, never executable
    assert "<pre>plain <script>" not in html           # not the old raw dump


def test_a_prosemirror_json_body_renders_formatted(client):
    doc = ('{"type":"doc","content":[{"type":"heading","attrs":{"level":2},'
           '"content":[{"type":"text","text":"Big"}]}]}')
    client.post("/bugreport/", data={"title": "t", "body": doc})
    html = client.get("/bugreport/").get_data(as_text=True)
    assert "<h2>Big</h2>" in html                       # the JSON body rendered as a heading


def test_editor_body_field_has_no_finding_id_so_autosave_stays_off(client):
    # bugreport is a form-submit host: the editor mounts with no data-finding-id, so the kit's per-block
    # autosave loop short-circuits (the finding-id gate) and the form submit is the only persistence.
    html = client.get("/bugreport/").get_data(as_text=True)
    assert "data-br-editor" in html
    assert "data-finding-id" not in html
