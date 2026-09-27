"""``lotek_kit.attackpath_map`` — the deterministic RankedPath -> attackpath/v1 transform (LOT-70).

The mapper reads its input STRUCTURALLY (it imports neither exploiteer nor vector), so these tests use
local fakes that mirror the exploiteer graph shapes exactly: a node is a tuple keyed by kind, an edge
exposes ``src``/``dst``/``kind``/``evidence``, a path exposes ``nodes``/``edges``/``goal``/``score``
(``exploiteer/exploiteer/graph.py:46-108``). If those shapes ever drift, these fakes are the contract to
update.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from lotek_kit.attackpath import SCHEMA_ID
from lotek_kit.attackpath_map import map_engagement, map_ranked_path, node_id

# ── structural fakes (mirror exploiteer/graph.py) ────────────────────────────────────────────────────
ATTACKER = ("attacker",)


def host(h: str) -> tuple:
    return ("host", h)


def service(h: str, port: int) -> tuple:
    return ("service", h, port)


@dataclass(frozen=True)
class FakeEdge:
    src: tuple
    dst: tuple
    kind: str = "attack"
    strength: int = 100
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class FakePath:
    nodes: tuple[tuple, ...]
    edges: tuple[FakeEdge, ...]
    score: int = 0
    goal: Any = None


def _two_hop_path(score: int = 40) -> FakePath:
    a, s1, h1, s2, h2 = ATTACKER, service("10.0.0.5", 445), host("10.0.0.5"), service("10.0.0.9", 3389), host("10.0.0.9")
    edges = (
        FakeEdge(a, s1, "reachable", 100, ("open tcp/445 on 10.0.0.5 from attacker",)),
        FakeEdge(s1, h1, "exploit", 70, ("CVE-2021-1234", "msf:exploit/windows/smb/ms17_010")),
        FakeEdge(h1, s2, "reachable", 100, ("open tcp/3389 on 10.0.0.9 from 10.0.0.5",)),
        FakeEdge(s2, h2, "exploit", 40, ("CVE-2020-5678",)),
    )
    return FakePath((a, s1, h1, s2, h2), edges, score=score, goal=h2)


# ── node id ──────────────────────────────────────────────────────────────────────────────────────────
def test_node_id_is_a_stable_string_from_the_tuple():
    assert node_id(ATTACKER) == "attacker"
    assert node_id(host("10.0.0.5")) == "host|10.0.0.5"
    assert node_id(service("10.0.0.5", 445)) == "service|10.0.0.5|445"


# ── zones: pure function of node kind ─────────────────────────────────────────────────────────────────
def test_zone_is_a_pure_function_of_node_kind_and_ordered():
    model = map_ranked_path(_two_hop_path())
    zones = {z["id"]: z["order"] for z in model["zones"]}
    assert set(zones) == {"attacker", "service", "host"}
    assert zones["attacker"] < zones["service"] < zones["host"]
    by_id = {n["id"]: n for n in model["nodes"]}
    assert by_id["attacker"]["zone"] == "attacker"
    assert by_id["service|10.0.0.5|445"]["zone"] == "service"
    assert by_id["host|10.0.0.5"]["zone"] == "host"


def test_node_label_and_ip_follow_the_tuple():
    by_id = {n["id"]: n for n in map_ranked_path(_two_hop_path())["nodes"]}
    assert by_id["attacker"]["label"] == "Attacker"
    assert by_id["host|10.0.0.5"]["label"] == "10.0.0.5"
    assert by_id["host|10.0.0.5"]["ip"] == "10.0.0.5"
    assert by_id["service|10.0.0.9|3389"]["label"] == "10.0.0.9:3389"
    assert by_id["service|10.0.0.9|3389"]["ip"] == "10.0.0.9"


# ── row = index along the ordered path ────────────────────────────────────────────────────────────────
def test_row_is_the_index_along_the_ordered_path():
    by_id = {n["id"]: n for n in map_ranked_path(_two_hop_path())["nodes"]}
    assert by_id["attacker"]["row"] == 0
    assert by_id["service|10.0.0.5|445"]["row"] == 1
    assert by_id["host|10.0.0.5"]["row"] == 2
    assert by_id["service|10.0.0.9|3389"]["row"] == 3
    assert by_id["host|10.0.0.9"]["row"] == 4


def test_edges_map_endpoints_and_reveal_at_destination_depth():
    edges = map_ranked_path(_two_hop_path())["edges"]
    pairs = {(e["from"], e["to"]): e for e in edges}
    assert ("attacker", "service|10.0.0.5|445") in pairs
    assert ("service|10.0.0.5|445", "host|10.0.0.5") in pairs
    # `at` follows the destination's depth so the timeline reveals hops in order.
    assert pairs[("attacker", "service|10.0.0.5|445")]["at"] == 1
    assert pairs[("service|10.0.0.9|3389", "host|10.0.0.9")]["at"] == 4
    # the exploit hop cites its CVE as the label
    assert pairs[("service|10.0.0.5|445", "host|10.0.0.5")]["label"] == "CVE-2021-1234"


# ── AC: byte-identical output ─────────────────────────────────────────────────────────────────────────
def test_same_input_produces_byte_identical_model():
    a = json.dumps(map_ranked_path(_two_hop_path()), sort_keys=False)
    b = json.dumps(map_ranked_path(_two_hop_path()), sort_keys=False)
    assert a == b
    # and stable across repeated maps of the very same object
    p = _two_hop_path()
    assert json.dumps(map_ranked_path(p)) == json.dumps(map_ranked_path(p))


def test_schema_id_is_unprefixed_and_carries_no_vector_name():
    model = map_ranked_path(_two_hop_path())
    assert model["schema"] == SCHEMA_ID == "attackpath/v1"
    assert "vector" not in json.dumps(model)


# ── AC: a malformed edge is dropped by normalize(), never raises ──────────────────────────────────────
def test_edge_to_a_node_not_in_the_path_is_dropped_not_raised():
    a, s1, h1 = ATTACKER, service("10.0.0.5", 445), host("10.0.0.5")
    dangling = host("10.9.9.9")  # never listed in `nodes`
    path = FakePath(
        nodes=(a, s1, h1),
        edges=(
            FakeEdge(a, s1, "reachable", 100, ("open",)),
            FakeEdge(s1, h1, "exploit", 70, ("CVE-1",)),
            FakeEdge(h1, dangling, "exploit", 40, ("CVE-DANGLING",)),  # endpoint not a node
        ),
        goal=h1,
    )
    model = map_ranked_path(path)  # must not raise
    tos = {e["to"] for e in model["edges"]}
    assert node_id(dangling) not in tos
    assert len(model["edges"]) == 2


@pytest.mark.parametrize(
    "path",
    [
        FakePath(nodes=(), edges=()),  # empty
        FakePath(nodes=(("attacker",),), edges=()),  # lone node, no edges
        FakePath(nodes=(("weird",), ("service",)), edges=(FakeEdge(("weird",), ("service",)),)),  # short tuples
    ],
)
def test_never_raises_on_degenerate_input(path):
    model = map_ranked_path(path)
    assert model["schema"] == SCHEMA_ID
    assert isinstance(model["nodes"], list) and isinstance(model["edges"], list)


# ── AC: the mapper introduces no external/fetchable reference ─────────────────────────────────────────
def test_mapper_introduces_no_external_url():
    """render_deliverable inlines its own CSS/JS and embeds the model as inert JSON, so the only way an
    external request could enter an exported deliverable is through the model. Clean scan data yields a
    model with no URL scheme in it — the mapper adds none of its own."""
    dumped = json.dumps(map_ranked_path(_two_hop_path()))
    assert "http://" not in dumped
    assert "https://" not in dumped


# ── engagement overview (multi-path) ──────────────────────────────────────────────────────────────────
def test_engagement_overview_unions_paths_and_dedupes_shared_nodes():
    p1 = _two_hop_path(score=70)
    # a second path that shares the attacker + first service, then diverges
    a, s1, h3 = ATTACKER, service("10.0.0.5", 445), host("10.0.0.7")
    p2 = FakePath(
        nodes=(a, s1, h3),
        edges=(FakeEdge(a, s1, "reachable", 100, ("open",)), FakeEdge(s1, h3, "exploit", 40, ("CVE-9",))),
        score=40,
        goal=h3,
    )
    model = map_engagement([p1, p2])
    ids = {n["id"] for n in model["nodes"]}
    assert "attacker" in ids and "host|10.0.0.7" in ids and "host|10.0.0.9" in ids
    # the shared service appears exactly once (dedup) ...
    assert sum(1 for n in model["nodes"] if n["id"] == "service|10.0.0.5|445") == 1
    # ... at the row it had in the FIRST path that reached it
    by_id = {n["id"]: n for n in model["nodes"]}
    assert by_id["service|10.0.0.5|445"]["row"] == 1
    assert model["meta"]["badge"] == "2 paths"


def test_engagement_floor_drops_paths_below_the_cutoff():
    p_strong = _two_hop_path(score=70)
    a, s1, h3 = ATTACKER, service("192.168.1.10", 22), host("192.168.1.10")
    p_weak = FakePath((a, s1, h3), (FakeEdge(a, s1, "reachable", 40, ("open",)), FakeEdge(s1, h3, "exploit", 30, ("CVE-W",))), score=30, goal=h3)
    model = map_engagement([p_strong, p_weak], floor=40)
    ids = {n["id"] for n in model["nodes"]}
    assert "host|10.0.0.9" in ids  # from the strong path
    assert "host|192.168.1.10" not in ids  # weak path filtered out
    assert model["meta"]["badge"] == "1 paths"


def test_engagement_overview_is_byte_identical_run_to_run():
    paths = [_two_hop_path(score=70), _two_hop_path(score=40)]
    assert json.dumps(map_engagement(paths)) == json.dumps(map_engagement(paths))
