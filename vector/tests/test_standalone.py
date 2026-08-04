"""Standalone app boots, seeds idempotently, and serves the pages."""

from __future__ import annotations

from vector.db import make_session_factory
from vector.seed import EXAMPLE_NAME, seed_defaults


def test_root_redirects_to_vector(client):
    r = client.get("/")
    assert r.status_code in (301, 302)
    assert "/vector" in r.headers["Location"]


def test_list_and_editor_pages_render(make_app):
    app = make_app(seed=True)
    c = app.test_client()
    assert c.get("/vector/").status_code == 200
    assert c.get("/vector/diagrams").status_code == 200  # nav alias
    assert c.get("/vector/new").status_code == 200
    diags = c.get("/vector/api/diagrams").get_json()["diagrams"]
    did = diags[0]["id"]
    body = c.get(f"/vector/edit/{did}").get_data(as_text=True)
    assert 'id="ved-model"' in body and "vector-editor.js" in body and "vector-viewer.js" in body
    # the embedded model must be valid JSON the browser can JSON.parse (regression: autoescaped &quot;)
    import json

    block = body.split('id="ved-model">', 1)[1].split("</script>", 1)[0]
    parsed = json.loads(block)
    assert parsed["nodes"] and parsed["zones"]


def test_seed_is_idempotent(app):
    cfg = app.extensions["vector"]
    sf = make_session_factory(cfg.engine)
    from vector.models import Diagram

    with sf() as s:
        seed_defaults(s)
        seed_defaults(s)  # second run must not duplicate
    with sf() as s:
        n = s.query(Diagram).filter(Diagram.name == EXAMPLE_NAME, Diagram.builtin.is_(True)).count()
    assert n == 1


def test_static_assets_served(make_app):
    app = make_app()
    c = app.test_client()
    for f in ("vector-viewer.js", "vector-viewer.css", "vector-editor.js", "vector-editor.css"):
        r = c.get(f"/vector/static/{f}")
        assert r.status_code == 200, f
