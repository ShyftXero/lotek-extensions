"""WS11 Phase B — live collaborative editing: server-side CRDT rooms.

Where Phase A (``scribble/autosave_api.py`` + ``scribble/collab/presence.py``) is debounced
last-writer-wins autosave with a soft "who's editing" signal, Phase B holds a real CRDT (`pycrdt`,
Python bindings for Yjs's Rust core) per ``(finding_id, block)`` "room" so concurrent edits from multiple
clients *merge* instead of clobbering each other.

Concurrency model (this is load-bearing — see below)
-----------------------------------------------------
`flask-sock` runs one OS thread per websocket, and a room's ``pycrdt.Doc`` is shared by every client in
that room. pycrdt transactions are **not thread-safe**: a ``Doc`` touched from two threads at once raises
a Rust ``PanicException`` ("Transaction is unsendable") or ``RuntimeError('Already in a transaction')``
(empirically, even with ``allow_multithreading=True``), and iterating a room's ``connections`` set while
another thread joins/leaves raises "Set changed size during iteration". So:

- Each :class:`Room` owns a ``threading.RLock`` held around **every** access to its ``ydoc`` /
  ``awareness`` and every mutation/iteration of its ``connections`` / ``transports``. The ``Doc`` is also
  created with ``allow_multithreading=True`` (belt-and-suspenders — the per-room lock is the actual fix).
- :class:`RoomManager` owns a re-entrant ``_lock`` guarding the ``_rooms``/``_timers`` registries and
  serializing the whole room *lifecycle* (open / connect / disconnect / close / persist / reconcile /
  idle-persist), so "is this room empty? → flush → evict" is atomic against "a new client connects" —
  no orphaned client, no evict-then-load-stale race, no double-INSERT on the unique ``(finding_id,
  block)``. Lock ordering is always manager → room (never the reverse), so no deadlock; the hot path
  (:meth:`Room.receive`, once per message) takes only the room lock and stays concurrent across rooms.

Architecture
------------
- :class:`Room` — one ``pycrdt.Doc`` + ``pycrdt.Awareness`` for a single ``(finding_id, block)``. It
  speaks the real Yjs wire protocol via pycrdt's ``pycrdt._sync``/``pycrdt._awareness`` helpers (sync
  step1/step2/update + awareness) — the same encoding a real ``y-websocket`` JS client uses, so the
  vendored client (``scribble/static/collab.js`` + ``scribble/static/lib/``) is wire-compatible with
  zero server changes. :meth:`Room.receive` is a pure ``(conn_id, message) -> {conn_id: [messages]}``
  transform (no I/O), which is what makes it fully testable headless (``tests/test_collab.py``).
- :class:`RoomManager` — process-wide registry + the DB-facing lifecycle: loading a room's prior
  ``CollabDoc.ydoc_state`` (or seeding from the finding's existing ``content_json[block]``), debounced
  *persistence* (durability: CRDT bytes -> ``CollabDoc.ydoc_state``), and *reconciliation* on room close
  (render the CRDT doc to ProseMirror JSON and write it into ``EngagementFinding.content_json``/
  ``content_html`` via ``content/render_html.render_block`` — the same walker Phase A's autosave uses).
- The ProseMirror JSON <-> Yjs mapping lives in ``scribble/collab/pm_yjs.py``.

Freshness vs. Phase A (why :meth:`RoomManager.open_room` re-checks ``content_json``): Phase A autosave is
always on and writes ``content_json`` but never ``ydoc_state``. If a collab session persisted
``ydoc_state``, then an autosave later edited the same block, then collab reopened, naively re-applying
the stale ``ydoc_state`` would resurrect the pre-autosave content and, on the next reconcile, silently
overwrite the autosave edit. So on open, when a ``ydoc_state`` row exists we compare its rendered content
against the current ``content_json[block]`` (both normalized) and re-seed from ``content_json`` when they
diverge — ``content_json`` is the source of truth the rest of the app agrees on.

Persistence vs. reconciliation are deliberately separate (:meth:`RoomManager.persist` vs.
:meth:`RoomManager.reconcile`): persistence happens often (survive a mid-session restart) without paying
to re-render HTML per keystroke-debounce, while reconciliation — refreshing the canonical
``content_json``/``content_html`` the report/autosave pipeline reads — only needs to happen when a room
closes. :meth:`RoomManager.close_room` does both.

Wire format note (matches pycrdt / y-protocols exactly): every message's first byte is a
``pycrdt.YMessageType`` (0 = SYNC, 1 = AWARENESS). For a SYNC message, ``message[1:]`` is handed to
``pycrdt.handle_sync_message``. For an AWARENESS message, ``message[1:]`` is a ``pycrdt.read_message``-
framed awareness update payload.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import pycrdt as Y
from flask import jsonify
from flask_sock import Sock
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from scribble.collab import pm_yjs
from scribble.content.render_html import render_block
from scribble.deps import get_config, open_session
from scribble.models import CollabDoc, EngagementFinding

_REGISTERED = False

# Debounce window for durability-only persistence (no reconciliation) while a room stays open.
DEFAULT_IDLE_PERSIST_SECONDS = 5.0

# Ceiling on simultaneously-open rooms — a memory-DoS guard (W5): without it, a client could open a room
# for every distinct (finding, block) it can name and pin unbounded CRDT docs in memory.
DEFAULT_MAX_ROOMS = 1000

# ``CollabDoc.block`` is ``String(64)``; the websocket route's ``<string:block>`` already excludes
# slashes, but validate length + charset before we ever create a room (W5).
_BLOCK_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def is_valid_block(block: Any) -> bool:
    """Whether ``block`` is a syntactically acceptable content-block name (<= 64 chars, safe charset)."""
    return isinstance(block, str) and _BLOCK_RE.fullmatch(block) is not None


@dataclass
class Room:
    """One live CRDT room for a single ``(finding_id, block)``.

    Thread-safety: ``_lock`` (an ``RLock``) is held around every ``ydoc``/``awareness`` access and every
    ``connections``/``transports`` mutation+iteration. See the module docstring's concurrency model."""

    finding_id: int
    block: str
    ydoc: Y.Doc
    awareness: Y.Awareness
    connections: set[str] = field(default_factory=set)
    # Live-transport handles (``.send(bytes)``-able objects), keyed by conn_id. Populated only by the
    # real websocket route (`register` below); pure protocol logic never touches this — tests exercise
    # `add_connection`/`receive`/`remove_connection` directly with plain string conn_ids and no transport.
    transports: dict[str, Any] = field(default_factory=dict, repr=False)
    dirty: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def add_connection(self, conn_id: str) -> list[bytes]:
        """Register ``conn_id`` as joined; returns the messages the caller must send back to it: our
        current document state (SYNC_STEP1, so the peer can reply with whatever we're missing) and a
        snapshot of any other peers' awareness state."""
        with self._lock:
            self.connections.add(conn_id)
            out = [Y.create_sync_message(self.ydoc)]
            other_client_ids = [c for c in self.awareness.states if c != self.awareness.client_id]
            if other_client_ids:
                out.append(
                    Y.create_awareness_message(self.awareness.encode_awareness_update(other_client_ids))
                )
            return out

    def remove_connection(self, conn_id: str) -> bool:
        """Drop ``conn_id`` + its transport; returns whether the room is now empty."""
        with self._lock:
            self.connections.discard(conn_id)
            self.transports.pop(conn_id, None)
            return not self.connections

    def set_transport(self, conn_id: str, transport: Any) -> None:
        with self._lock:
            if conn_id in self.connections:
                self.transports[conn_id] = transport

    def receive(self, conn_id: str, raw: bytes) -> dict[str, list[bytes]]:
        """Process one incoming message from ``conn_id``.

        Returns ``{target_conn_id: [messages to send]}`` — direct replies to the sender (e.g. a
        SYNC_STEP2 answer to a SYNC_STEP1) and/or broadcasts of new updates to every *other* connected
        peer. Performs no I/O: callers (the websocket route, or a test) dispatch the returned messages.
        The whole body runs under the room lock so the shared ``ydoc``/``awareness`` and the
        ``connections`` snapshot for broadcasting are never touched concurrently."""
        outbox: dict[str, list[bytes]] = {}
        if not raw:
            return outbox
        msg_type, payload = raw[0], raw[1:]

        with self._lock:
            if msg_type == Y.YMessageType.SYNC:
                state_before = self.ydoc.get_state()
                reply = Y.handle_sync_message(payload, self.ydoc)
                if reply is not None:
                    outbox.setdefault(conn_id, []).append(reply)
                state_after = self.ydoc.get_state()
                if state_after != state_before:
                    delta = self.ydoc.get_update(state_before)
                    update_message = Y.create_update_message(delta)
                    for other in self.connections:
                        if other != conn_id:
                            outbox.setdefault(other, []).append(update_message)
                    self.dirty = True

            elif msg_type == Y.YMessageType.AWARENESS:
                data = Y.read_message(payload)
                self.awareness.apply_awareness_update(data, origin=conn_id)
                for other in self.connections:
                    if other != conn_id:
                        outbox.setdefault(other, []).append(raw)

        return outbox

    def dispatch(self, outbox: dict[str, list[bytes]]) -> None:
        """Send an :meth:`receive` outbox to the live transports. A failing/dead peer is dropped and
        cleaned up rather than allowed to propagate an exception that would kill the calling client's
        socket loop (W3). Sends happen outside the lock (a slow socket must not block the whole room);
        the transport snapshot and the failure cleanup are taken under it."""
        with self._lock:
            targets = [(cid, self.transports.get(cid), outbox[cid]) for cid in outbox]
        failed: list[str] = []
        for cid, transport, messages in targets:
            if transport is None:
                continue
            try:
                for message in messages:
                    transport.send(message)
            except Exception:  # noqa: BLE001 - a dead peer must not take out the others
                logger.debug("[collab] dropping peer {} on send failure", cid)
                failed.append(cid)
        if failed:
            with self._lock:
                for cid in failed:
                    self.connections.discard(cid)
                    self.transports.pop(cid, None)

    def render(self) -> dict:
        """The room's current CRDT content, rendered back to ProseMirror JSON."""
        with self._lock:
            return pm_yjs.ydoc_to_doc(self.ydoc)

    def state_bytes(self) -> bytes:
        """The full CRDT update from doc creation — a self-contained snapshot suitable for
        ``CollabDoc.ydoc_state`` (replaying it into a fresh ``pycrdt.Doc`` reconstructs the document)."""
        with self._lock:
            return self.ydoc.get_update()

    def is_empty(self) -> bool:
        with self._lock:
            return not self.connections


def _artifact_url(artifact_id: int) -> str:
    """Best-effort artifact URL for the CRDT-doc-derived HTML cache (mirrors
    ``scribble.autosave_api._artifact_url`` — WS5's real serve route may not be wired at render time)."""
    try:
        prefix = get_config().url_prefix
    except RuntimeError:  # pragma: no cover - defensive; no app context
        prefix = "/scribble"
    return f"{prefix}/api/artifacts/{artifact_id}/raw"


def authorize_connection(finding_id: int, block: str) -> bool:
    """Gate a websocket connection (W5). Rejects invalid block names, then consults an optional host
    hook ``get_config().extras['collab_authorize']`` — a ``(finding_id, block) -> bool`` callable the
    embedding app (e.g. Lotek) sets to enforce its own auth/RBAC. Absent a hook, connections are allowed
    (the route still inherits whatever session/CSRF the host mounts in front of it, and
    :meth:`RoomManager.open_room` still refuses to create a room for a nonexistent finding)."""
    if not is_valid_block(block):
        return False
    try:
        hook = get_config().extras.get("collab_authorize")
    except RuntimeError:  # pragma: no cover - defensive; no app context
        hook = None
    if hook is not None:
        try:
            return bool(hook(finding_id, block))
        except Exception:  # noqa: BLE001 - a throwing host hook denies, never crashes the socket
            logger.exception("[collab] collab_authorize hook raised; denying connection")
            return False
    return True


class RoomManager:
    """Process-wide registry of open :class:`Room` objects, keyed by ``(finding_id, block)``.

    ``_lock`` (re-entrant) guards ``_rooms``/``_timers`` and serializes the room lifecycle so eviction
    is atomic against connection. See the module docstring's concurrency model."""

    def __init__(
        self,
        *,
        idle_persist_seconds: float = DEFAULT_IDLE_PERSIST_SECONDS,
        max_rooms: int = DEFAULT_MAX_ROOMS,
    ) -> None:
        self._rooms: dict[tuple[int, str], Room] = {}
        self._timers: dict[tuple[int, str], threading.Timer] = {}
        self._lock = threading.RLock()
        self.idle_persist_seconds = idle_persist_seconds
        self.max_rooms = max_rooms

    # ------------------------------------------------------------------ lifecycle

    def open_room(self, session, finding_id: int, block: str) -> Room:
        """Get the room for ``(finding_id, block)``, creating + loading it if absent.

        Raises ``LookupError`` if the finding doesn't exist (W5: never spin up a room — and pin a CRDT
        doc in memory — for a bogus id) and ``RuntimeError`` if the room cap is reached. Held entirely
        under ``_lock`` (including DB I/O) so open/connect/evict are mutually atomic; ``_lock`` is
        re-entrant so :meth:`connect` can call this while holding it."""
        key = (finding_id, block)
        with self._lock:
            room = self._rooms.get(key)
            if room is not None:
                return room

            finding = session.get(EngagementFinding, finding_id)
            if finding is None:
                raise LookupError(f"finding {finding_id} does not exist")
            if len(self._rooms) >= self.max_rooms:
                raise RuntimeError(f"collab room cap reached ({self.max_rooms})")

            ydoc = Y.Doc(allow_multithreading=True)
            row = session.execute(
                select(CollabDoc).where(CollabDoc.finding_id == finding_id, CollabDoc.block == block)
            ).scalar_one_or_none()
            existing_doc = (finding.content_json or {}).get(block)

            if row is not None and row.ydoc_state:
                ydoc.apply_update(row.ydoc_state)
                # C2 freshness: if content_json diverged from the persisted CRDT state (a Phase A
                # autosave edited the block between collab sessions), content_json is authoritative
                # — re-seed from it so the autosave edit isn't resurrected-away on the next reconcile.
                if existing_doc is not None and pm_yjs.ydoc_to_doc(ydoc) != pm_yjs.normalize_doc(
                    existing_doc
                ):
                    ydoc = Y.Doc(allow_multithreading=True)
                    pm_yjs.doc_to_ydoc(existing_doc, ydoc)
            elif existing_doc:
                # Brand-new room: seed from whatever's already in content_json so opening live collab on
                # an already-authored block doesn't blank it out from under the author.
                pm_yjs.doc_to_ydoc(existing_doc, ydoc)

            room = Room(finding_id=finding_id, block=block, ydoc=ydoc, awareness=Y.Awareness(ydoc))
            self._rooms[key] = room
            return room

    def get_room(self, finding_id: int, block: str) -> Room | None:
        with self._lock:
            return self._rooms.get((finding_id, block))

    def connect(self, session, finding_id: int, block: str, conn_id: str) -> list[bytes]:
        """Open (if needed) + join a room atomically. Held under ``_lock`` so a concurrent last-client
        :meth:`disconnect` can't evict the room in the window between open and join (W1/W2)."""
        with self._lock:
            room = self.open_room(session, finding_id, block)
            return room.add_connection(conn_id)

    def receive(self, finding_id: int, block: str, conn_id: str, raw: bytes) -> dict[str, list[bytes]]:
        # Hot path: only a brief manager-lock lookup, then the room lock inside Room.receive.
        room = self.get_room(finding_id, block)
        if room is None:
            return {}
        return room.receive(conn_id, raw)

    def register_transport(self, finding_id: int, block: str, conn_id: str, transport: Any) -> None:
        room = self.get_room(finding_id, block)
        if room is not None:
            room.set_transport(conn_id, transport)

    def dispatch(self, finding_id: int, block: str, outbox: dict[str, list[bytes]]) -> None:
        room = self.get_room(finding_id, block)
        if room is not None:
            room.dispatch(outbox)

    def disconnect(self, session, finding_id: int, block: str, conn_id: str) -> bool:
        """Drop ``conn_id``. If it was the last connection, flush (persist + reconcile) and evict — all
        under ``_lock`` so the emptiness check, flush, and eviction can't interleave with a new
        :meth:`connect` (W2). Returns whether the room was closed."""
        key = (finding_id, block)
        with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return False
            room.remove_connection(conn_id)
            if not room.is_empty():
                return False
            # Last one out, and no connect can interleave (we hold _lock): flush + evict.
            self._flush_locked(session, room)
            self._cancel_timer_locked(key)
            self._rooms.pop(key, None)
            return True

    def close_room(self, session, finding_id: int, block: str) -> bool:
        """Unconditionally flush (persist + reconcile) and evict the room. Used on last-client-leave and
        directly by tests to exercise end-of-session behavior without a full connection lifecycle."""
        key = (finding_id, block)
        with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return False
            self._flush_locked(session, room)
            self._cancel_timer_locked(key)
            self._rooms.pop(key, None)
            return True

    # ------------------------------------------------------------------ persistence + reconciliation

    def _flush_locked(self, session, room: Room) -> None:
        """persist + reconcile a room. Caller MUST hold ``_lock``."""
        self._persist_room(session, room)
        self._reconcile_room(session, room)

    def persist(self, session, finding_id: int, block: str) -> CollabDoc | None:
        """Durability only: write the room's current CRDT state to ``CollabDoc.ydoc_state``. Does
        **not** touch ``EngagementFinding.content_json``/``content_html`` — see :meth:`reconcile`."""
        with self._lock:
            room = self._rooms.get((finding_id, block))
            if room is None:
                return None
            return self._persist_room(session, room)

    def _persist_room(self, session, room: Room) -> CollabDoc:
        row = session.execute(
            select(CollabDoc).where(
                CollabDoc.finding_id == room.finding_id, CollabDoc.block == room.block
            )
        ).scalar_one_or_none()
        state = room.state_bytes()
        now_ms = int(time.time() * 1000)
        if row is None:
            row = CollabDoc(
                finding_id=room.finding_id, block=room.block, ydoc_state=state, updated_at_ms=now_ms
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # W2: a concurrent writer already inserted the unique (finding_id, block) row — fall
                # back to updating it instead of crashing. (Lifecycle serialization makes this rare,
                # but the idle-persist Timer fires on its own thread, so keep the guard.)
                session.rollback()
                row = session.execute(
                    select(CollabDoc).where(
                        CollabDoc.finding_id == room.finding_id, CollabDoc.block == room.block
                    )
                ).scalar_one()
                row.ydoc_state = state
                row.updated_at_ms = now_ms
                session.commit()
        else:
            row.ydoc_state = state
            row.updated_at_ms = now_ms
            session.commit()
        room.dirty = False
        return row

    def reconcile(self, session, finding_id: int, block: str) -> bool:
        with self._lock:
            room = self._rooms.get((finding_id, block))
            if room is None:
                return False
            return self._reconcile_room(session, room)

    def _reconcile_room(self, session, room: Room) -> bool:
        """Render the room's CRDT doc to ProseMirror JSON and write it back into
        ``EngagementFinding.content_json[block]`` / ``content_html[block]`` so the autosave/report
        pipeline sees the final, merged document. ``content_json``/``content_html`` are plain ``JSON``
        columns (not ``MutableDict``) — SQLAlchemy only detects a *new* object assigned, so we reassign
        the whole dict rather than mutating a key in place (RAILS §8)."""
        finding = session.get(EngagementFinding, room.finding_id)
        if finding is None:
            return False

        doc = room.render()
        html = render_block(doc, artifact_url=_artifact_url)

        content_json = dict(finding.content_json or {})
        content_json[room.block] = doc
        finding.content_json = content_json

        content_html = dict(finding.content_html or {})
        content_html[room.block] = html
        finding.content_html = content_html

        session.commit()
        return True

    # ------------------------------------------------------------------ idle durability timer

    def schedule_idle_persist(
        self, finding_id: int, block: str, session_factory, *, delay: float | None = None
    ) -> None:
        """(Re)start a debounce timer that calls :meth:`persist` (not :meth:`reconcile`) after ``delay``
        seconds of no further calls — best-effort durability against a server restart mid-session. Safe
        to call on every incoming update; each call cancels+restarts the timer. A no-op (no DB write) if
        the room is gone or has no unpersisted changes (:attr:`Room.dirty`)."""
        key = (finding_id, block)
        wait = self.idle_persist_seconds if delay is None else delay

        def fire() -> None:
            with self._lock:
                self._timers.pop(key, None)
                room = self._rooms.get(key)
                if room is None or not room.dirty:
                    return
                with session_factory() as session:
                    self._persist_room(session, room)

        timer = threading.Timer(wait, fire)
        timer.daemon = True
        with self._lock:
            old = self._timers.pop(key, None)
            self._timers[key] = timer
            if old is not None:
                old.cancel()
            timer.start()

    def _cancel_timer_locked(self, key: tuple[int, str]) -> None:
        """Cancel + drop a room's idle timer. Caller MUST hold ``_lock``."""
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()


# Module-level singleton — one process, one registry (mirrors scribble/collab/presence.py's `registry`).
manager = RoomManager()


def register(api_bp, bp) -> None:
    """Mount the Phase B collab websocket route + a small HTTP status fallback.

    Signature matches the driver's ``register(api_bp, bp)`` wiring convention (see
    ``plans/CONTRACTS.md`` / ``scribble/__init__.py:_wire_feature_routes``, which the driver — not this
    workstream — updates to call this). Idempotent: guarded by ``_REGISTERED`` so re-invoking across
    multiple test-app fixtures in one process never double-registers routes on the shared blueprint
    objects (Flask forbids adding routes to an already-registered blueprint)."""
    global _REGISTERED
    if _REGISTERED:
        return

    sock = Sock()

    @sock.route("/ws/findings/<int:finding_id>/blocks/<string:block>", bp=bp)
    def collab_ws(ws, finding_id: int, block: str) -> None:  # pragma: no cover - needs a real socket
        if not authorize_connection(finding_id, block):
            ws.close()
            return
        conn_id = uuid.uuid4().hex
        try:
            with open_session() as session:
                initial = manager.connect(session, finding_id, block, conn_id)
        except (LookupError, RuntimeError):
            # Unknown finding (W5) or room cap reached — refuse without spinning up a room.
            ws.close()
            return
        manager.register_transport(finding_id, block, conn_id, ws)
        for message in initial:
            try:
                ws.send(message)
            except Exception:  # noqa: BLE001
                break
        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                # One malformed frame or one dead peer must not kill this client's loop (W3).
                try:
                    outbox = manager.receive(finding_id, block, conn_id, raw)
                    manager.dispatch(finding_id, block, outbox)
                    manager.schedule_idle_persist(finding_id, block, open_session)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[collab] error handling frame for finding {} block {}", finding_id, block
                    )
                    continue
        finally:
            with open_session() as session:
                manager.disconnect(session, finding_id, block, conn_id)

    @api_bp.get("/findings/<int:finding_id>/blocks/<string:block>/collab-status")
    def collab_status(finding_id: int, block: str):
        room = manager.get_room(finding_id, block)
        lock_ctx = room._lock if room is not None else contextlib.nullcontext()
        with lock_ctx:
            connections = len(room.connections) if room is not None else 0
        return jsonify(ok=True, active=room is not None, connections=connections)

    _REGISTERED = True
