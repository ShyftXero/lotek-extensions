"""In-memory editing-presence registry (WS4 Phase A).

Tracks, per ``(finding_id, block)``, which users have recently heartbeat-ed an edit session on that
content block ("X is editing"). This is intentionally *not* persisted and *not* a CRDT: Phase A is
last-writer-wins autosave (see ``scribble/autosave_api.py``) guarded only by this soft, advisory signal
so collaborators can see they're stepping on each other. True concurrent merge is Phase B (WS11: Yjs
client <-> ``pycrdt`` server, persisted via the ``CollabDoc`` model already frozen in ``models.py``).

Kept deliberately framework-simple: a TTL'd dict behind a lock, exposed over plain HTTP polling
endpoints registered onto the shared API blueprint via :func:`register`. No ``flask-socketio`` dependency
for Phase A, even though it's present in the project (PLAN.md §8 mentions socketio as an option); polling
every few seconds is sufficient for "who's editing" and keeps this module trivially testable.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from flask import jsonify, request

# How long a heartbeat keeps a user "active" without a follow-up ping. Clients should heartbeat at
# roughly half this interval (see scribble/static/editor.js PRESENCE_HEARTBEAT_MS).
DEFAULT_TTL_SECONDS = 20.0


@dataclass
class PresenceRegistry:
    """Thread-safe, in-process presence tracker keyed by ``(finding_id, block)``."""

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _state: dict[tuple[int, str], dict[str, float]] = field(default_factory=dict, repr=False, compare=False)

    def heartbeat(self, finding_id: int, block: str, user: str, *, now: float | None = None) -> list[dict]:
        """Record that ``user`` is actively editing ``block``; returns the current active list."""
        now = time.time() if now is None else now
        key = (finding_id, block)
        with self._lock:
            bucket = self._state.setdefault(key, {})
            bucket[user] = now
        return self.active(finding_id, block, now=now)

    def leave(self, finding_id: int, block: str, user: str) -> list[dict]:
        """Explicitly drop ``user`` (e.g. on blur/unload) instead of waiting for TTL expiry."""
        key = (finding_id, block)
        with self._lock:
            bucket = self._state.get(key)
            if bucket is not None:
                bucket.pop(user, None)
        return self.active(finding_id, block)

    def active(self, finding_id: int, block: str, *, now: float | None = None) -> list[dict]:
        """Current non-stale editors of ``block``, most-recently-seen first. Purges stale entries."""
        now = time.time() if now is None else now
        key = (finding_id, block)
        with self._lock:
            bucket = self._state.get(key)
            if not bucket:
                return []
            stale = [u for u, seen in bucket.items() if now - seen > self.ttl_seconds]
            for u in stale:
                del bucket[u]
            if not bucket:
                self._state.pop(key, None)
                return []
            editors = [
                {"user": u, "last_seen": seen, "seconds_ago": round(now - seen, 1)}
                for u, seen in bucket.items()
            ]
        editors.sort(key=lambda e: e["seconds_ago"])
        return editors


# Module-level singleton — Phase A has one process, one registry. A future multi-worker deployment
# would swap this for a Redis-backed implementation behind the same interface (see module docstring).
# NOTE: deliberately not named `presence` — that would collide with (and shadow) this module's own name
# when re-exported from scribble/collab/__init__.py (`from scribble.collab.presence import presence`
# rebinds the package attribute `scribble.collab.presence` from "the submodule" to "this instance",
# breaking `import scribble.collab.presence as x`). Callers reach it as
# `scribble.collab.presence.registry` or via the submodule import.
registry = PresenceRegistry()

_REGISTERED = False


def register(api_bp, bp) -> None:
    """Attach presence heartbeat/read routes onto the shared API blueprint.

    Signature matches the driver's WS4 wiring convention (``register(api_bp, bp)``); ``bp`` (the UI
    blueprint) isn't needed by Phase A presence and is accepted only for symmetry / future use.
    Idempotent so re-invoking (e.g. once per test-app fixture) doesn't double-register routes on the
    shared blueprint object — see ``tests/test_editor.py``.
    """
    global _REGISTERED
    if _REGISTERED:
        return

    @api_bp.post("/findings/<int:finding_id>/blocks/<string:block>/presence")
    def presence_heartbeat(finding_id: int, block: str):
        body = request.get_json(silent=True) or {}
        user = _clean_user(body.get("user"))
        if body.get("leave"):
            editors = registry.leave(finding_id, block, user)
        else:
            editors = registry.heartbeat(finding_id, block, user)
        return jsonify(ok=True, editors=editors, count=len(editors))

    @api_bp.get("/findings/<int:finding_id>/blocks/<string:block>/presence")
    def presence_list(finding_id: int, block: str):
        editors = registry.active(finding_id, block)
        return jsonify(ok=True, editors=editors, count=len(editors))

    _REGISTERED = True


def _clean_user(raw) -> str:
    user = str(raw or "").strip()
    return user or "anonymous"
