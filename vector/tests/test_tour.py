"""Tour mode + the bundled lotek walkthrough: schema fields, the example, its claims sidecar, the seed
upsert, and a static CSP check of the viewer runtime. The rendered behaviour is in test_e2e_tour.py."""

from __future__ import annotations

import json
import re
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from vector.db import make_session_factory
from vector.models import Diagram, UserPref
from vector.schema import normalize
from vector.seed import BUILTINS, EXAMPLE_NAME, TOUR_FILE, TOUR_NAME, _model, load_example, seed_defaults

PKG = Path(__file__).resolve().parent.parent / "vector"
EXAMPLES = files("vector").joinpath("examples")
SIDECAR = "lotek-walkthrough.claims.toml"


def _doc(**phase) -> dict:
    return {"zones": [{"id": "z", "title": "Z"}], "nodes": [{"id": "a", "zone": "z"}],
            "phases": [{"n": 1, "title": "p", **phase}]}


# ── schema ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [("tour", "tour"), (" TOUR ", "tour"), ("attack", None),
                                           ("", None), (None, None), (["tour"], None), (1, None)])
def test_meta_mode_only_records_tour(raw, expected):
    meta = normalize({"meta": {"title": "t", "mode": raw}})["meta"]
    assert meta.get("mode") == expected


def test_meta_mode_absent_keeps_existing_documents_unchanged():
    assert "mode" not in normalize({"meta": {"title": "t"}})["meta"]
    assert "mode" not in normalize(_model())["meta"]


def test_activate_at_is_preserved():
    """Regression: _norm_node dropped activateAt, so the seeded backup C2 (activateAt 15) never woke up
    in the viewer — its stored document had lost the field."""
    raw = next(n for n in _model()["nodes"] if "activateAt" in n)
    node = next(n for n in normalize(_model())["nodes"] if n["id"] == raw["id"])
    assert node["activateAt"] == raw["activateAt"] == 15


@pytest.mark.parametrize("value, kept", [(0, 0), (7, 7), ("4", 4), (-1, None), ("x", None), (None, None),
                                         (True, None), ([3], None)])
def test_activate_at_coercion(value, kept):
    m = normalize({"zones": [{"id": "z"}], "nodes": [{"id": "a", "zone": "z", "activateAt": value}]})
    assert m["nodes"][0].get("activateAt") == kept


@pytest.mark.parametrize("href", ["/docs", "/docs#dataflow-3-runners", "/docs/api", "#step-2", "guide.html",
                                  "../docs/x", "?q=1"])
def test_links_keep_relative_and_docs_targets(href):
    phase = normalize(_doc(links=[{"label": "L", "href": href}]))["phases"][0]
    assert phase["links"] == [{"label": "L", "href": href}]


@pytest.mark.parametrize("href", ["http://evil", "https://evil.example/docs", "javascript:alert(1)",
                                  "JavaScript:alert(1)", "java\tscript:alert(1)", " javascript:alert(1)",
                                  "//evil.example/docs", "/\\evil.example", "data:text/html,x",
                                  "vbscript:x", "/other", "/docsx", "mailto:a@b", "",
                                  "javascript\u00a0:alert(1)", "\u2028javascript:alert(1)",
                                  "\ufeffjavascript:alert(1)", "java\u200bscript:alert(1)"])
def test_links_drop_offsite_targets(href):
    phase = normalize(_doc(links=[{"label": "L", "href": href}]))["phases"][0]
    assert "links" not in phase


def test_links_label_defaults_capped_and_malformed_dropped():
    links = [{"href": "/docs"}, "not-a-dict", {"label": "no href"}] + [{"label": str(i), "href": "#x"}
                                                                       for i in range(20)]
    out = normalize(_doc(links=links))["phases"][0]["links"]
    assert out[0] == {"label": "/docs", "href": "/docs"}
    # _TOUR_MAX_LINKS (8) caps the RAW list, so the two malformed entries still spend a slot
    assert [link["label"] for link in out] == ["/docs", "0", "1", "2", "3", "4"]


@pytest.mark.parametrize("src", ["/vector/static/shot.png", "img/step-1.png",
                                 "data:image/png;base64,iVBORw0KGgo=", "data:image/webp;base64,UklGR"])
def test_image_keeps_same_origin_and_raster_data(src):
    assert normalize(_doc(image=src))["phases"][0]["image"] == src


@pytest.mark.parametrize("src", ["http://evil/x.png", "https://evil/x.png", "//evil/x.png",
                                 "javascript:alert(1)", "data:image/svg+xml;base64,PHN2Zz4=",
                                 "data:text/html;base64,PHNjcmlwdD4=", "data:image/png;base64,<script>",
                                 "data:image/png,rawbytes", "/\\evil/x.png", "a b.png"])
def test_image_drops_everything_else(src):
    assert "image" not in normalize(_doc(image=src))["phases"][0]


# ── the bundled example ────────────────────────────────────────────────────────────────────────


def test_example_is_package_data():
    assert EXAMPLES.joinpath(TOUR_FILE).is_file()
    assert EXAMPLES.joinpath(SIDECAR).is_file()


def test_example_normalizes_with_nothing_dropped():
    raw = load_example(TOUR_FILE)
    doc = normalize(raw)
    assert doc["meta"]["mode"] == "tour"
    assert len(doc["zones"]) == 4
    assert [z["title"] for z in doc["zones"]] == ["Setup", "Scan", "Triage", "Deliver"]
    assert len(doc["nodes"]) == len(raw["nodes"]) == 12
    assert len(doc["edges"]) == len(raw["edges"]) == 10  # zero dropped edges
    assert [p["targets"] for p in doc["phases"]] == [p.get("targets", []) for p in raw["phases"]]
    # every node sits in a real zone (not reassigned) and every link survived sanitisation
    assert all(n["zone"] == r["zone"] for n, r in zip(doc["nodes"], raw["nodes"], strict=True))
    for p, r in zip(doc["phases"], raw["phases"], strict=True):
        assert p.get("links", []) == r.get("links", [])
    assert normalize(doc) == doc  # canonical: round-trips


def test_example_steps_and_vocabulary():
    doc = normalize(load_example(TOUR_FILE))
    steps = [p for p in doc["phases"] if not p.get("intro")]
    assert [p["n"] for p in steps] == list(range(1, 10))
    assert all(p["targets"] and p["links"] for p in steps)
    assert all(link["href"].startswith("/docs#dataflow-") for p in steps for link in p["links"])
    assert {e["kind"] for e in doc["edges"]} <= set(doc["style"]["edgeKinds"])
    assert {s["state"] for n in doc["nodes"] for s in n["states"]} <= set(doc["style"]["nodeStates"])
    accents = {z["accent"] for z in doc["zones"]} | {k["accent"] for k in doc["style"]["edgeKinds"].values()}
    assert accents <= {"cyan", "green", "violet"}
    text = json.dumps(doc).lower()
    for word in ("red team", "blue team", "exploit", "owned", "beacon"):
        assert word not in text


# ── the claims sidecar ─────────────────────────────────────────────────────────────────────────


def _sidecar() -> dict:
    return tomllib.loads(EXAMPLES.joinpath(SIDECAR).read_text(encoding="utf-8"))


def test_sidecar_parses_and_uses_only_the_documented_keys():
    data = _sidecar()
    assert set(data) == {"claim"}
    for claim in data["claim"]:
        assert set(claim) <= {"id", "anchors", "describes"}
        assert re.fullmatch(r"DOC-VECTOR-TOUR-\d{2}", claim["id"])
        assert claim["anchors"] and all(re.fullmatch(r"[a-z]+=\S+", a) for a in claim["anchors"])


def test_sidecar_ids_are_unique_and_cover_every_step():
    ids = [c["id"] for c in _sidecar()["claim"]]
    assert len(ids) == len(set(ids))
    steps = [p["n"] for p in normalize(load_example(TOUR_FILE))["phases"] if not p.get("intro")]
    assert ids == [f"DOC-VECTOR-TOUR-{n:02d}" for n in steps]


def test_sidecar_describes_a_sibling_that_exists():
    for claim in _sidecar()["claim"]:
        assert EXAMPLES.joinpath(claim["describes"]).is_file(), claim


def test_no_yaml_in_vector():
    root = PKG.parent
    found = [p for p in root.rglob("*") if p.suffix in (".yml", ".yaml") and ".venv" not in p.parts]
    assert found == []


# ── seeding upserts ────────────────────────────────────────────────────────────────────────────


def _sf(app):
    return make_session_factory(app.extensions["vector"].engine)


def _builtin(s, name) -> Diagram:
    return s.query(Diagram).filter(Diagram.builtin.is_(True), Diagram.name == name).one()


def test_seed_creates_every_builtin(app):
    with _sf(app)() as s:
        seed_defaults(s)
    with _sf(app)() as s:
        names = {d.name for d in s.query(Diagram).filter(Diagram.builtin.is_(True))}
        tour = json.loads(_builtin(s, TOUR_NAME).model_json)
    assert names == {name for name, _ in BUILTINS} == {TOUR_NAME, EXAMPLE_NAME}
    assert tour == normalize(load_example(TOUR_FILE))


def test_seed_updates_a_builtin_whose_bundled_document_changed(app):
    with _sf(app)() as s:
        seed_defaults(s)
        row = _builtin(s, TOUR_NAME)
        row_id = row.id
        row.model_json = json.dumps(normalize({"meta": {"title": "stale"}}))  # an older bundled version
        s.commit()
    with _sf(app)() as s:
        seed_defaults(s)
    with _sf(app)() as s:
        row = _builtin(s, TOUR_NAME)
        assert row.id == row_id  # updated in place, not re-created
        assert json.loads(row.model_json) == normalize(load_example(TOUR_FILE))
        assert s.query(Diagram).filter(Diagram.name == TOUR_NAME).count() == 1


def test_seed_is_a_no_op_when_unchanged(app, monkeypatch):
    with _sf(app)() as s:
        seed_defaults(s)
    with _sf(app)() as s:
        before = {d.id: (d.model_json, d.updated_at) for d in s.query(Diagram)}
        commits = []
        monkeypatch.setattr(s, "commit", lambda: commits.append(1))
        seed_defaults(s)
        assert commits == [] and not s.dirty and not s.new
    with _sf(app)() as s:
        assert {d.id: (d.model_json, d.updated_at) for d in s.query(Diagram)} == before


def test_seed_never_touches_user_diagrams_or_prefs(app):
    import uuid

    uid = uuid.uuid7()
    with _sf(app)() as s:
        # a user's own diagram that happens to share the builtin's name, and their "hide" preference
        mine = Diagram(name=TOUR_NAME, builtin=False, owner_id=uid, model_json='{"meta":{"title":"mine"}}')
        s.add_all([mine, UserPref(owner_id=uid, hide_builtin_diagrams=True)])
        s.commit()
        mine_id = mine.id
    with _sf(app)() as s:
        seed_defaults(s)
        seed_defaults(s)
    with _sf(app)() as s:
        assert s.get(Diagram, mine_id).model_json == '{"meta":{"title":"mine"}}'
        assert s.query(UserPref).filter(UserPref.owner_id == uid).one().hide_builtin_diagrams is True
        assert s.query(Diagram).filter(Diagram.name == TOUR_NAME, Diagram.builtin.is_(True)).count() == 1


# ── CSP: static checks on the runtime (the browser check is test_e2e_tour.py) ───────────────────


def _code(js: str) -> str:
    """The runtime with comments stripped, so prose about style="" doesn't count."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(line.split("//", 1)[0] if line.lstrip().startswith("//") else line
                     for line in js.splitlines())


def test_viewer_writes_no_style_attribute_and_never_evals():
    code = _code((PKG / "static" / "vector-viewer.js").read_text(encoding="utf-8"))
    assert 'style="' not in code and "style='" not in code
    assert not re.search(r"setAttribute\(\s*['\"]style", code)
    assert not re.search(r"\beval\s*\(|new\s+Function\b|setTimeout\(\s*['\"]|setInterval\(\s*['\"]", code)


def test_deliverable_boots_from_the_json_island_without_an_inline_parse_script():
    tpl = (PKG / "templates" / "vector" / "deliverable.html.j2").read_text(encoding="utf-8")
    assert 'type="application/json" id="vap-model"' in tpl
    assert "JSON.parse" not in tpl
