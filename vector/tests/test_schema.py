"""schema.normalize — defaults, coercion, caps, and the never-raise contract."""

from __future__ import annotations

import pytest

from vector import schema
from vector.schema import blank_model, normalize
from vector.seed import _model


@pytest.mark.parametrize("bad", [None, 1, "x", [], True, {"zones": "not-a-list"}, {"nodes": [1, 2, "x"]}])
def test_normalize_never_raises(bad):
    out = normalize(bad)
    assert out["schema"] == "vector.attackpath/v1"
    for key in ("meta", "zones", "boundaries", "nodes", "edges", "phases"):
        assert key in out


def test_blank_model_is_valid_and_has_intro():
    m = blank_model("Hi")
    assert m["meta"]["title"] == "Hi"
    assert any(p.get("intro") for p in m["phases"])


def test_dangling_edges_dropped():
    m = normalize({
        "zones": [{"id": "z", "title": "Z"}],
        "nodes": [{"id": "a", "zone": "z"}],
        "edges": [{"id": "e1", "from": "a", "to": "ghost", "kind": "attack", "at": 1},
                  {"id": "e2", "from": "a", "to": "a", "kind": "attack", "at": 1}],
    })
    ids = [e["id"] for e in m["edges"]]
    assert "e1" not in ids and "e2" in ids  # ghost endpoint dropped, valid one kept


def test_duplicate_node_ids_deduped():
    m = normalize({"zones": [{"id": "z", "title": "Z"}],
                   "nodes": [{"id": "a", "zone": "z"}, {"id": "a", "zone": "z"}]})
    assert len(m["nodes"]) == 1


def test_states_sorted_and_bad_state_dropped():
    m = normalize({"zones": [{"id": "z", "title": "Z"}],
                   "nodes": [{"id": "a", "zone": "z", "states": [
                       {"at": 5, "state": "owned"}, {"state": "x"}, {"at": 2, "label": "t"}]}]})
    states = m["nodes"][0]["states"]
    assert [s["at"] for s in states] == [2, 5]  # the at-less state dropped; sorted ascending


def test_targets_filtered_to_real_nodes():
    m = normalize({"zones": [{"id": "z", "title": "Z"}],
                   "nodes": [{"id": "a", "zone": "z"}],
                   "phases": [{"n": 1, "title": "p", "targets": ["a", "ghost", 3, None]}]})
    assert m["phases"][0]["targets"] == ["a"]


def test_zone_id_out_of_vocab_reassigned():
    # a node pointing at a non-existent zone is reassigned to the first real zone (never dangles)
    m = normalize({"zones": [{"id": "z", "title": "Z"}], "nodes": [{"id": "a", "zone": "nope"}]})
    assert m["nodes"][0]["zone"] == "z"


def test_length_caps_applied():
    huge = "x" * 10000
    m = normalize({"meta": {"title": huge}, "zones": [], "nodes": [], "phases": [{"n": 1, "desc": huge}]})
    assert len(m["meta"]["title"]) <= schema._MED
    assert len(m["phases"][0]["desc"]) <= schema._LONG


def test_normalize_is_idempotent_on_reference():
    once = normalize(_model())
    twice = normalize(once)
    assert once == twice  # export -> import round-trip is identity for a well-formed doc


def test_reference_shape():
    m = normalize(_model())
    assert len(m["zones"]) == 5
    assert len(m["nodes"]) == 21
    assert len(m["edges"]) == 28
    # intro + 16 phases
    assert len(m["phases"]) == 17
    assert any(p.get("intro") for p in m["phases"])
