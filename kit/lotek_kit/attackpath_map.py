"""Deterministic ``RankedPath`` -> ``attackpath/v1`` mapper (LOT-70 / LOT-48/2).

A pure transform: same input graph -> byte-identical document, no authoring judgment, no model call. It
turns the exploit-path graph output of ``exploiteer`` (a ``RankedPath``) into an ``attackpath/v1`` model
that the Vector viewer renders. It lives in the kit, the neutral ground both sides meet on, beside the
``attackpath/v1`` contract itself (:mod:`lotek_kit.attackpath`).

**It imports neither exploiteer nor vector.** The kit's hard rule is that it never imports lotek or an
extension (``kit/README.md``), so the source graph is read STRUCTURALLY, not by importing its classes:

- a ``RankedPath`` is anything exposing ``nodes`` (a sequence of node tuples), ``edges`` (a sequence of
  edge objects), and optionally ``goal`` / ``score``;
- a ``Node`` is a tuple whose first element is its kind: ``("attacker",)`` / ``("host", h)`` /
  ``("service", h, port)`` (``exploiteer/exploiteer/graph.py:46-55``);
- an ``Edge`` exposes ``src`` / ``dst`` (node tuples) and optionally ``kind`` / ``evidence``
  (``graph.py:90-96``).

**Model only, never render.** The mapper stops at a normalized dict. Whether the render step imports
``vector.render.render_deliverable`` directly or round-trips the vector machine API is open item O1
(architecture; security-invariant review: Cerberus / Lord_Nikon; ZeroCool confirms) and belongs to the
G2 bridge orchestrator (LOT-71). Keeping this render-free leaves O1 fully open.

The transform rules (issue LOT-70, accepted LOT-48 plan annex §14):

- **Node id** — ``"|".join(map(str, node))``. A stable string from the tuple, so the same graph yields
  the same ids run to run (idempotency / dedup).
- **Zone** — a pure function of node kind (``node[0]``): ``attacker`` / ``service`` / ``host``. Columns
  are ordered ``attacker < service < host``, the direction of a first hop.
- **Row** — a ``RankedPath`` carries no per-node depth (``graph.py:100``), so row is the node's index
  along the ordered path (start = 0, +1 per hop). Deterministic from the stable DFS edge order
  (``graph.py:195``). Across paths (the engagement overview) the first path that reaches a node fixes
  its row.
- **Edges** — each ``Edge`` becomes ``{from, to, ...}``. :func:`lotek_kit.attackpath.normalize` drops
  any edge whose endpoint is not a real node id (fail-safe) and never raises.
"""

from __future__ import annotations

from typing import Any

from lotek_kit.attackpath import SCHEMA_ID, normalize

# Zone catalog: a pure function of node kind. Column order is the direction of a first hop
# (attacker reaches a service, an exploit lands a host); lateral movement routes back through the same
# columns. Anything the three exploiteer kinds do not cover falls after them, ordered by its own name so
# the result stays deterministic without a hardcoded verdict about where it goes.
_ZONE_ORDER: dict[str, int] = {"attacker": 0, "service": 1, "host": 2}
_ZONE_TITLE: dict[str, str] = {"attacker": "Attacker", "service": "Services", "host": "Hosts"}
_UNKNOWN_ORDER = 90  # unknown kinds sort after the known three, before the MAX_ZONES cap


def node_id(node: Any) -> str:
    """Stable id for a node tuple: ``"|".join(map(str, node))``.

    ``("attacker",)`` -> ``"attacker"``; ``("host", "10.0.0.5")`` -> ``"host|10.0.0.5"``;
    ``("service", "10.0.0.5", 445)`` -> ``"service|10.0.0.5|445"``.
    """
    return "|".join(map(str, node))


def _kind(node: Any) -> str:
    try:
        return str(node[0])
    except (TypeError, IndexError, KeyError):
        return ""


def _zone_id(node: Any) -> str:
    """A node's zone id is its kind, verbatim. Falls back to ``host`` only for a truly empty tuple, so a
    degenerate node still lands in a real column rather than an id-less one."""
    return _kind(node) or "host"


def _label_ip(node: Any) -> tuple[str, str]:
    """``(label, ip)`` for a node. attacker -> ``("Attacker", "")``; host ``h`` -> ``(h, h)``;
    service ``(h, port)`` -> ``(f"{h}:{port}", h)``. Unknown shapes fall back to the raw id."""
    kind = _kind(node)
    if kind == "attacker":
        return "Attacker", ""
    if kind == "host":
        host = str(node[1]) if len(node) > 1 else ""
        return host, host
    if kind == "service":
        host = str(node[1]) if len(node) > 1 else ""
        port = str(node[2]) if len(node) > 2 else ""
        return (f"{host}:{port}" if port else host), host
    return node_id(node), ""


def _edge_label(edge: Any) -> str:
    """The first named artifact on the edge (a CVE/KEV id for an exploit hop, the observed-port note for
    a reachability hop), or the edge kind if it carries none. Cites evidence, invents nothing."""
    evidence = getattr(edge, "evidence", ()) or ()
    for item in evidence:
        text = str(item).strip()
        if text:
            return text
    return str(getattr(edge, "kind", "") or "")


def _zones_for(kinds: set[str]) -> list[dict[str, Any]]:
    """One zone per node kind present, ordered ``attacker < service < host < (other, by name)``."""
    ordered = sorted(kinds, key=lambda k: (_ZONE_ORDER.get(k, _UNKNOWN_ORDER), k))
    return [
        {"id": k, "title": _ZONE_TITLE.get(k, k or "Nodes"), "accent": "slate", "order": i}
        for i, k in enumerate(ordered)
    ]


def _phases_by_depth(row_of: dict[str, int]) -> list[dict[str, Any]]:
    """A deterministic timeline: one phase per depth. Phase ``n`` reveals the nodes at row ``n`` (edges
    carry their own ``at`` = destination depth). Phase 0 is the intro (the attacker's starting position).
    Titles are structural (``Start`` / ``Depth n``), never authored prose."""
    max_row = max([0, *row_of.values()])
    phases: list[dict[str, Any]] = []
    for n in range(max_row + 1):
        targets = sorted(nid for nid, row in row_of.items() if row == n)
        phase: dict[str, Any] = {"n": n, "title": "Start" if n == 0 else f"Depth {n}", "targets": targets}
        if n == 0:
            phase["intro"] = True
        phases.append(phase)
    return phases


def _assemble(
    row_of: dict[str, int],
    node_objs: dict[str, Any],
    order: list[str],
    edges_acc: list[tuple[str, str, Any]],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Build the raw model from accumulated nodes/edges and hand it to ``normalize`` (which fixes key
    order, drops dangling edges, caps lengths, and never raises)."""
    zones = _zones_for({_zone_id(node_objs[nid]) for nid in order})
    nodes: list[dict[str, Any]] = []
    for nid in order:
        node = node_objs[nid]
        label, ip = _label_ip(node)
        nodes.append({"id": nid, "label": label, "ip": ip, "zone": _zone_id(node), "row": row_of[nid]})
    edges: list[dict[str, Any]] = []
    for i, (frm, to, edge) in enumerate(edges_acc):
        edges.append(
            {
                "id": f"e{i}",
                "from": frm,
                "to": to,
                "kind": str(getattr(edge, "kind", "") or "attack"),
                "at": row_of.get(to, 1),
                "label": _edge_label(edge),
            }
        )
    model = {
        "schema": SCHEMA_ID,
        "meta": meta,
        "zones": zones,
        "nodes": nodes,
        "edges": edges,
        "phases": _phases_by_depth(row_of),
    }
    return normalize(model)


def _accumulate(paths: list[Any]) -> tuple[dict[str, int], dict[str, Any], list[str], list[tuple[str, str, Any]]]:
    """Walk paths in order, recording each node's row (first path to reach it wins) and each unique
    ``(from, to)`` edge (first occurrence wins). Deterministic for a fixed path order."""
    row_of: dict[str, int] = {}
    node_objs: dict[str, Any] = {}
    order: list[str] = []
    edges_acc: list[tuple[str, str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    for path in paths:
        for i, node in enumerate(getattr(path, "nodes", ()) or ()):
            nid = node_id(node)
            if nid not in row_of:
                row_of[nid] = i
                node_objs[nid] = node
                order.append(nid)
        for edge in getattr(path, "edges", ()) or ():
            src = getattr(edge, "src", None)
            dst = getattr(edge, "dst", None)
            frm = node_id(src) if src is not None else ""
            to = node_id(dst) if dst is not None else ""
            key = (frm, to)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges_acc.append((frm, to, edge))
    return row_of, node_objs, order, edges_acc


def map_ranked_path(path: Any, *, title: str | None = None, subtitle: str = "") -> dict:
    """Map one ``RankedPath`` to a normalized ``attackpath/v1`` document (the per-chain diagram).

    ``title`` defaults to ``Attack path to <goal label>``; ``meta.badge`` carries the weakest-edge score
    when the path exposes one. Both are structural, deterministic, and evidence-derived.
    """
    row_of, node_objs, order, edges_acc = _accumulate([path])
    goal = getattr(path, "goal", None)
    score = getattr(path, "score", None)
    if title is None:
        title = f"Attack path to {_label_ip(goal)[0]}" if goal is not None else "Attack path"
    meta: dict[str, Any] = {"title": title, "subtitle": subtitle}
    if score is not None:
        meta["badge"] = f"score {score}"
    return _assemble(row_of, node_objs, order, edges_acc, meta)


def map_engagement(
    paths: Any,
    *,
    floor: int = 0,
    title: str = "Engagement attack paths",
    subtitle: str = "",
) -> dict:
    """Map every ranked path scoring at or above ``floor`` into one overview ``attackpath/v1`` document.

    Paths are consumed in the order given (``exploiteer.graph.ranked_paths`` returns them most-real
    first). A node's row is fixed by the first path that reaches it; a ``(from, to)`` edge is emitted
    once. ``floor`` is the weakest-edge score cutoff (0 keeps all).
    """
    kept = [p for p in paths if (getattr(p, "score", 0) or 0) >= floor]
    row_of, node_objs, order, edges_acc = _accumulate(kept)
    meta: dict[str, Any] = {"title": title, "subtitle": subtitle, "badge": f"{len(kept)} paths"}
    return _assemble(row_of, node_objs, order, edges_acc, meta)
