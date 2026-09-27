# Plan: feat/kit-attackpath-mapper

- **Branch:** `feat/kit-attackpath-mapper`  (worktree: `.claude/worktrees/vector-mapper`, off `main`)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose
LOT-70 (LOT-48/2). The Vector layout mapper: a pure, deterministic transform from an exploiteer
`RankedPath` (exploit-path graph output) to an `attackpath/v1` document. No authoring judgment, no model
call, same input -> byte-identical output. Consumed by the G2 bridge orchestrator (LOT-71), which owns
the render step and its coupling (open item O1).

## Approach
- Home: `kit/lotek_kit/attackpath_map.py`, beside the ported `attackpath.py`. The kit is the neutral
  ground both consumers meet on and it already owns the `attackpath/v1` contract (`normalize`,
  `blank_model`, `SCHEMA_ID`).
- **No imports of exploiteer or vector.** The mapper reads a duck-typed `RankedPath` (`.nodes`,
  `.edges`, `.goal`; `Node` is a tuple; `Edge` has `.src`/`.dst`/`.kind`/`.strength`/`.evidence`). This
  keeps the kit's hard constraint ("never imports lotek or an extension") intact.
- **Model only, no render.** The mapper stops at a normalized `attackpath/v1` dict. Whether the render
  step imports `vector.render.render_deliverable` or round-trips the vector machine API is open item O1
  (security-invariant review: Cerberus / Lord_Nikon; ZeroCool confirms) and belongs to LOT-71. Keeping
  the mapper render-free leaves O1 fully open.

### Transform rules (from the issue / annex §14)
- **Node id:** `"|".join(map(str, node))` — stable string from the tuple. Same graph -> same ids.
- **Zone:** pure function of node kind (`node[0]`): `attacker` / `service` / `host`. Column order is
  fixed `attacker=0, service=1, host=2` (the direction of a first hop: attacker -> service -> host).
- **Label / ip:** attacker -> `Attacker`; `("host", h)` -> label `h`, ip `h`;
  `("service", h, p)` -> label `h:p`, ip `h`.
- **Row:** RankedPath carries no per-node depth, so row = index of the node along the ordered path
  (start=0, +1 per hop). Deterministic from the stable DFS edge order.
- **Edges:** each `Edge` -> `{from, to, kind, at, label}`; `normalize()` drops any edge whose endpoint
  is not a real node id (fail-safe), never raises.
- **Phases:** deterministic minimal timeline — an intro phase plus one per hop, so the interactive
  reveal works; targets = the hop's destination node.
- **Multi-path (engagement overview):** union of nodes/edges across all ranked paths above a score
  floor, in ranked order; a node's row is taken from the first path that contains it (stable).

## Done
- [ ] `kit/lotek_kit/attackpath_map.py` — `map_ranked_path`, `map_engagement`, `node_id`, zone helpers.
- [ ] `kit/tests/test_attackpath_map.py` — byte-identical determinism, node ids/zones/rows, malformed
      edge dropped by normalize, no external URLs introduced by the mapper.

## Remaining
- [ ] `uvx ruff check kit` + `cd kit && uv run --extra dev pyrefly check` + `pytest -q` green.
- [ ] Reviews + `--ack-*` markers; PR into `main`.
- [ ] LOT-71 wires the mapper into the G2 bridge and resolves O1 (render coupling).

## Notes / gotchas
- **byte-identical:** dict key order is insertion order and `json.dumps` preserves it; `normalize`
  builds keys in a fixed order; the mapper iterates ordered tuples. So identical input -> identical
  bytes with no extra effort, and a test pins it.
- The mapper does NOT render, so AC "render_deliverable output is self-contained" is proven at the
  render seam (vector's own `test_render`/`test_print_still`, and the LOT-71 end-to-end). The mapper's
  contribution is that its model introduces no fetchable/external references — pinned by a test.
- No `vector.` prefix anywhere: the kit's `SCHEMA_ID` is the un-prefixed `attackpath/v1`.
