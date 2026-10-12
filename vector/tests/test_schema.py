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


# ext#250 — non-intro phase n=0 data-loss bug
def test_non_intro_phase_n_zero_clamped_to_one():
    """A non-intro phase whose n was set to 0 (the intro's slot) must be clamped to 1 on
    normalize.  Before the fix, n=0 was accepted on non-intro phases, causing two phases to
    share slot 0: the intro's data got clobbered in the viewer's phaseMap (last one wins),
    and the editor rendered the phase as an 'intro slide' with no Phase # field —
    making the change irreversible."""
    m = normalize({
        "phases": [
            {"n": 0, "intro": True},          # real intro
            {"n": 0, "title": "Lateral movement", "tactics": []},  # accidentally set to 0
        ]
    })
    phase_ns = [p["n"] for p in m["phases"]]
    # The non-intro phase must NOT be allowed to claim slot 0
    assert phase_ns.count(0) == 1, f"Expected exactly one phase with n=0, got {phase_ns}"
    # The non-intro phase should be clamped to n >= 1
    non_intro = [p for p in m["phases"] if not p.get("intro")]
    assert all(p["n"] >= 1 for p in non_intro), f"Non-intro phase got n=0: {non_intro}"


def test_intro_phase_n_zero_preserved():
    """The intro phase is allowed to have n=0 — do not break normal intro normalization."""
    m = normalize({
        "phases": [
            {"n": 0, "intro": True},
            {"n": 1, "title": "Phase 1"},
        ]
    })
    intro = next(p for p in m["phases"] if p.get("intro"))
    assert intro["n"] == 0
