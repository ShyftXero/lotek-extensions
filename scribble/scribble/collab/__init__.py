"""Collaboration primitives for rich-text editing.

**Phase A** (``scribble/collab/presence.py``): lightweight, in-memory "who's editing" presence per
(finding, block). Ephemeral, single-process, no persistence — matches the ``EditPresence`` note in
PLAN.md §4 ("ephemeral, in-memory/redis-optional — no schema churn"). Clients poll; there is no websocket
requirement for Phase A. This keeps working standalone wherever the Phase B CRDT path below isn't active
(e.g. a block nobody has opened for live co-editing yet).

**Phase B** (``scribble/collab/crdt.py`` + ``scribble/collab/pm_yjs.py``, WS11): Yjs-protocol-compatible
CRDT sync over a websocket (`pycrdt` on the server), persisted via the ``CollabDoc`` model
(``scribble/models.py``). This *complements* Phase A rather than replacing it: presence is still used for
the lightweight editing indicator, while ``collab/crdt.py`` owns real concurrent merge for any block a
live collab session has been opened on, reconciling back into ``EngagementFinding.content_json``/
``content_html`` (the same columns Phase A autosave writes) once the session ends. Both
``scribble.collab.presence.register`` and ``scribble.collab.crdt.register`` are imported directly by the
driver's ``scribble/__init__.py:_wire_feature_routes`` (not through this package ``__init__``), matching
the existing WS4 wiring convention.
"""

from __future__ import annotations

from scribble.collab.presence import PresenceRegistry, register, registry

__all__ = ["PresenceRegistry", "registry", "register"]
