"""Collaboration primitives for rich-text editing.

**Phase A** (``fraction/collab/presence.py``): lightweight, in-memory "who's editing" presence per
(finding, block). Ephemeral, single-process, no persistence — matches the ``EditPresence`` note in
PLAN.md §4 ("ephemeral, in-memory/redis-optional — no schema churn"). Clients poll; there is no websocket
requirement for Phase A. This keeps working standalone wherever the Phase B CRDT path below isn't active
(e.g. a block nobody has opened for live co-editing yet).

**Phase B** (``fraction/collab/crdt.py`` + ``fraction/collab/pm_yjs.py``, WS11): Yjs-protocol-compatible
CRDT sync over a websocket (`pycrdt` on the server), persisted via the ``CollabDoc`` model
(``fraction/models.py``). This *complements* Phase A rather than replacing it: presence is still used for
the lightweight editing indicator, while ``collab/crdt.py`` owns real concurrent merge for any block a
live collab session has been opened on, reconciling back into ``EngagementFinding.content_json``/
``content_html`` (the same columns Phase A autosave writes) once the session ends. Both
``fraction.collab.presence.register`` and ``fraction.collab.crdt.register`` are imported directly by the
driver's ``fraction/__init__.py:_wire_feature_routes`` (not through this package ``__init__``), matching
the existing WS4 wiring convention.
"""

from __future__ import annotations

from fraction.collab.presence import PresenceRegistry, register, registry

__all__ = ["PresenceRegistry", "registry", "register"]
