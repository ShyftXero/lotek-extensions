"""The ``attackpath/v1`` model — validation + normalization.

Ported verbatim from ``vector/vector/schema.py`` (map #148, decision 13). It lives here, in a package
neither lotek core nor any extension owns, so that core can emit a job-derived scaffold **without
knowing that any consuming extension exists** — the coupling this library was created to dissolve.

One function matters: :func:`normalize`. It takes an arbitrary (possibly hostile, possibly
browser-authored) dict and returns a clean, canonical document — coercing types, capping lengths,
dropping malformed entries, and filling defaults. It **never raises**: a bad field degrades to a default
rather than breaking the store/render (the same "degrade gracefully" contract lotek's parser DSL uses),
because this runs server-side on user-influenceable input and its output is embedded verbatim into an
exported HTML deliverable.

.. warning::
   ``normalize`` is a NORMALIZER, not a validator. It coerces and caps; it never rejects. Do not put it
   in front of a trust boundary and call it a gate — a whole-document replace filtered only by
   ``normalize`` accepts any content the caps allow.

**The schema id, and why the rename is staged.** ``SCHEMA_ID`` is the un-prefixed ``attackpath/v1``: the
``vector.`` prefix names an extension that map #148 deletes. Documents carrying the old id are accepted
on read (:data:`LEGACY_SCHEMA_IDS`) and ``normalize`` rewrites the id on the way out. **Nothing consumes
this module yet, and that is deliberate** — while vector is still a mounted extension its own normalizer
re-stamps ``vector.attackpath/v1`` (``vector/vector/schema.py:314``), so a core caller switched to this
constant early would assert an id the production write path immediately discards. Core adopts it in the
same change that retires vector (#159), not before.

The visual *style* catalogs (edge kinds, node states, roles, tactic kinds) live in the JS runtime, which
merges ``model.style`` over its baked-in defaults. Here we only sanity-check ``style`` is an object and
pass it through — the renderer owns the vocabulary. Note that this makes ``style`` an unvalidated
passthrough: a caller that must bound what a document can carry cannot rely on this module to do it.
"""

from __future__ import annotations

from typing import Any

SCHEMA_ID = "attackpath/v1"

#: Ids from before the rename. Accepted on read so a stored document keeps loading; never emitted.
LEGACY_SCHEMA_IDS = ("vector.attackpath/v1",)

#: Every id :func:`normalize` will accept as "this is one of ours".
SUPPORTED_SCHEMA_IDS = (SCHEMA_ID, *LEGACY_SCHEMA_IDS)

# Bounds — keep an exported deliverable finite and a browser-authored doc from ballooning.
MAX_ZONES = 40
MAX_NODES = 600
MAX_EDGES = 1500
MAX_PHASES = 200
MAX_STATES_PER_NODE = 60
MAX_TARGETS_PER_PHASE = 80
MAX_TACTICS_PER_PHASE = 12
MAX_RAIL_LABELS = 24

_SHORT = 120  # ids, accents, kinds, routes
_MED = 400  # labels, titles, ips, domains, mitre
_LONG = 4000  # descriptions, queries, notes, findings
_ACCENTS = {"red", "orange", "cyan", "amber", "green", "violet", "slate"}
_ROUTES = {"flow", "arcTop", "arcBot", "intra"}


def _s(v: Any, cap: int = _MED, default: str = "") -> str:
    """Coerce to a length-capped string; None/non-scalar -> default."""
    if v is None:
        return default
    if isinstance(v, bool):  # bool is an int subclass — never want "True"/"False" text here
        return default
    if not isinstance(v, (str, int, float)):
        return default
    return str(v)[:cap]


def _i(v: Any, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
    """Coerce to an int; non-numeric -> default; clamp to [lo, hi] when given."""
    try:
        if isinstance(v, bool):
            return default
        n = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _b(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _slug(v: Any, fallback: str) -> str:
    """A safe id: printable, capped, no whitespace collapse issues. Empty -> fallback."""
    s = _s(v, _SHORT).strip()
    return s or fallback


def _accent(v: Any) -> str:
    s = _s(v, _SHORT).strip().lower()
    return s if s in _ACCENTS else "slate"


def _route(v: Any) -> str:
    s = _s(v, _SHORT).strip()
    return s if s in _ROUTES else "flow"


def _norm_state(raw: Any) -> dict | None:
    d = _dict(raw)
    if not d:
        return None
    at = _i(d.get("at"), default=-1, lo=0)
    if at < 0:
        return None  # a state with no valid phase index is meaningless — drop it
    out: dict[str, Any] = {"at": at}
    state = _s(d.get("state"), _SHORT).strip()
    if state:
        out["state"] = state
    label = _s(d.get("label"), _MED).strip()
    if label:
        out["label"] = label
    return out


def _norm_reip(raw: Any) -> dict | None:
    d = _dict(raw)
    if not d:
        return None
    return {
        "at": _i(d.get("at"), default=0, lo=0),
        "ip": _s(d.get("ip"), _MED),
        "domain": _s(d.get("domain"), _MED),
    }


def _norm_zone(raw: Any, idx: int) -> dict:
    d = _dict(raw)
    return {
        "id": _slug(d.get("id"), f"zone{idx}"),
        "title": _s(d.get("title"), _MED, default=f"Zone {idx + 1}"),
        "subtitle": _s(d.get("subtitle"), _MED),
        "accent": _accent(d.get("accent")),
        "order": _i(d.get("order"), default=idx, lo=0, hi=MAX_ZONES),
    }


def _norm_boundary(raw: Any) -> dict | None:
    d = _dict(raw)
    if not d:
        return None
    out: dict[str, Any] = {"top": _s(d.get("top"), _MED), "bottom": _s(d.get("bottom"), _MED)}
    after = _s(d.get("afterZone"), _SHORT).strip()
    if after:
        out["afterZone"] = after
    if "x" in d:
        out["x"] = _i(d.get("x"), default=0)
    if not out.get("afterZone") and "x" not in out and not out["top"] and not out["bottom"]:
        return None
    return out


def _norm_node(raw: Any, idx: int, zone_ids: set[str]) -> dict | None:
    d = _dict(raw)
    node_id = _slug(d.get("id"), f"node{idx}")
    zone = _s(d.get("zone"), _SHORT).strip()
    out: dict[str, Any] = {
        "id": node_id,
        "label": _s(d.get("label"), _MED, default=node_id),
        "ip": _s(d.get("ip"), _MED),
        "domain": _s(d.get("domain"), _MED),
        "zone": zone if zone in zone_ids else (next(iter(zone_ids)) if zone_ids else zone),
        "row": _i(d.get("row"), default=0, lo=0, hi=200),
        "context": _b(d.get("context")),
    }
    role = _s(d.get("role"), _SHORT).strip()
    if role:
        out["role"] = role
    dual = _s(d.get("dualIp"), _MED).strip()
    if dual:
        out["dualIp"] = dual
    reip = _norm_reip(d.get("reIp"))
    if reip is not None:
        out["reIp"] = reip
    states = []
    for s_raw in _list(d.get("states"))[:MAX_STATES_PER_NODE]:
        st = _norm_state(s_raw)
        if st is not None:
            states.append(st)
    states.sort(key=lambda s: s["at"])
    out["states"] = states
    return out


def _norm_edge(raw: Any, idx: int, node_ids: set[str]) -> dict | None:
    d = _dict(raw)
    frm = _s(d.get("from"), _SHORT).strip()
    to = _s(d.get("to"), _SHORT).strip()
    # An edge whose endpoints aren't real nodes can't be drawn — drop it rather than render a dangling
    # line (defensive: import from a hand-edited or partial doc).
    if frm not in node_ids or to not in node_ids:
        return None
    out: dict[str, Any] = {
        "id": _slug(d.get("id"), f"edge{idx}"),
        "from": frm,
        "to": to,
        "kind": _slug(d.get("kind"), "attack"),
        "at": _i(d.get("at"), default=1, lo=0),
        "route": _route(d.get("route")),
    }
    label = _s(d.get("label"), _MED).strip()
    if label:
        out["label"] = label
    if "offset" in d:
        out["offset"] = _i(d.get("offset"), default=0, lo=-400, hi=400)
    if "lane" in d:
        out["lane"] = _i(d.get("lane"), default=0, lo=-400, hi=1200)
    return out


def _norm_tactic(raw: Any) -> dict | None:
    d = _dict(raw)
    label = _s(d.get("label"), _MED).strip()
    if not label:
        return None
    return {"label": label, "kind": _slug(d.get("kind"), "attack")}


def _norm_blue(raw: Any) -> dict | None:
    d = _dict(raw)
    if not d:
        return None
    return {
        "tool": _s(d.get("tool"), _MED),
        "finding": _s(d.get("finding"), _LONG),
        "query": _s(d.get("query"), _LONG),
        "seen": _s(d.get("seen"), _LONG),
        "note": _s(d.get("note"), _LONG),
        "gap": _b(d.get("gap")),
    }


def _norm_phase(raw: Any, idx: int, node_ids: set[str]) -> dict:
    d = _dict(raw)
    out: dict[str, Any] = {"n": _i(d.get("n"), default=idx, lo=0)}
    if _b(d.get("intro")):
        out["intro"] = True
    out["title"] = _s(d.get("title"), _MED)
    tactics = []
    for t_raw in _list(d.get("tactics"))[:MAX_TACTICS_PER_PHASE]:
        t = _norm_tactic(t_raw)
        if t is not None:
            tactics.append(t)
    out["tactics"] = tactics
    out["mitre"] = _s(d.get("mitre"), _MED)
    out["desc"] = _s(d.get("desc"), _LONG)
    out["targets"] = [
        t for t in (_s(x, _SHORT).strip() for x in _list(d.get("targets"))[:MAX_TARGETS_PER_PHASE])
        if t and t in node_ids
    ]
    out["watch"] = _s(d.get("watch"), _LONG)
    note = _s(d.get("note"), _LONG).strip()
    if note:
        out["note"] = note
    blue = _norm_blue(d.get("blue"))
    if blue is not None:
        out["blue"] = blue
    return out


def _norm_meta(raw: Any) -> dict:
    d = _dict(raw)
    intro = _dict(d.get("intro"))
    out_intro = {
        "eyebrow": _s(intro.get("eyebrow"), _MED),
        "objective": _s(intro.get("objective"), _LONG),
        "readingNotes": _s(intro.get("readingNotes"), _LONG),
        "note": _s(intro.get("note"), _LONG),
    }
    return {
        "title": _s(d.get("title"), _MED, default="Untitled attack path"),
        "subtitle": _s(d.get("subtitle"), _MED),
        "badge": _s(d.get("badge"), _MED),
        "railLabels": [_s(x, _MED) for x in _list(d.get("railLabels"))[:MAX_RAIL_LABELS]],
        "intro": out_intro,
    }


def is_supported_schema_id(value: Any) -> bool:
    """True when ``value`` names a document shape this module understands.

    :func:`normalize` deliberately accepts anything and stamps :data:`SCHEMA_ID` over it, so it can
    never tell a caller "that was not an attack path". A caller importing a foreign document needs to
    make that distinction BEFORE normalizing, and this is how.
    """
    return isinstance(value, str) and value in SUPPORTED_SCHEMA_IDS


def normalize(model: Any) -> dict:
    """Return a canonical, safe ``attackpath/v1`` document. Never raises.

    The returned ``schema`` key is always :data:`SCHEMA_ID`, whatever the input claimed — including
    a legacy ``vector.``-prefixed id, which is how a stored document migrates by being read.
    """
    m = _dict(model)

    zones_raw = _list(m.get("zones"))[:MAX_ZONES]
    zones = [_norm_zone(z, i) for i, z in enumerate(zones_raw)]
    zones.sort(key=lambda z: z["order"])
    zone_ids = {z["id"] for z in zones}

    boundaries = [b for b in (_norm_boundary(x) for x in _list(m.get("boundaries"))) if b is not None]

    nodes_raw = _list(m.get("nodes"))[:MAX_NODES]
    nodes: list[dict] = []
    seen_nodes: set[str] = set()
    for i, n in enumerate(nodes_raw):
        node = _norm_node(n, i, zone_ids)
        if node is None:
            continue
        if node["id"] in seen_nodes:  # de-dupe id collisions deterministically (first wins)
            continue
        seen_nodes.add(node["id"])
        nodes.append(node)
    node_ids = set(seen_nodes)

    edges: list[dict] = []
    seen_edges: set[str] = set()
    for i, e in enumerate(_list(m.get("edges"))[:MAX_EDGES]):
        edge = _norm_edge(e, i, node_ids)
        if edge is None:
            continue
        if edge["id"] in seen_edges:
            edge["id"] = f"{edge['id']}_{i}"
        seen_edges.add(edge["id"])
        edges.append(edge)

    phases = [_norm_phase(p, i, node_ids) for i, p in enumerate(_list(m.get("phases"))[:MAX_PHASES])]

    out: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "meta": _norm_meta(m.get("meta")),
        "zones": zones,
        "boundaries": boundaries,
        "nodes": nodes,
        "edges": edges,
        "phases": phases,
    }
    style = _dict(m.get("style"))
    if style:
        out["style"] = style  # passthrough; the runtime owns the vocabulary + merges over its defaults
    return out


def blank_model(title: str = "Untitled attack path") -> dict:
    """A minimal valid empty diagram (one intro phase, no zones/nodes yet)."""
    return normalize(
        {
            "meta": {"title": title},
            "zones": [],
            "nodes": [],
            "edges": [],
            "phases": [{"n": 0, "intro": True}],
        }
    )
