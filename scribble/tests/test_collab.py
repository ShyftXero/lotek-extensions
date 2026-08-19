"""Tests for WS11 Phase B: server-side CRDT co-editing (scribble/collab/crdt.py + pm_yjs.py).

Per RAILS §4, these assert the real end-state, not a proxy for it:

- ``test_pm_yjs_round_trip_preserves_structure`` -- the ProseMirror JSON <-> Yjs mapping is lossless
  across every node type the frozen content schema defines.
- ``test_two_clients_concurrent_edits_merge_for_real`` / ``test_same_position_concurrent_edits_merge`` --
  two independent, in-process ``pycrdt.Doc`` "clients" exchange sync/update messages *through the actual
  ``Room``/``RoomManager`` protocol code* (no shortcuts, no shared Python object standing in for the
  network) and converge to a document containing *both* concurrent edits -- proof this is a real CRDT
  merge, not last-writer-wins (which would silently drop one side).
- ``test_persist_and_reload_recovers_prior_content`` -- ``CollabDoc.ydoc_state`` durably round-trips
  through a simulated room eviction (server restart / idle recycle).
- ``test_close_room_reconciles_content_json_and_html`` -- closing a room writes the CRDT doc's rendered
  ProseMirror JSON/HTML into ``EngagementFinding.content_json``/``content_html``.
- ``test_persist_alone_does_not_reconcile_content`` -- the guard test: it exercises ``persist()`` in
  isolation (exactly what's left if ``reconcile()`` were accidentally dropped from ``close_room``) and
  proves the "content_json reflects the merged doc" assertion goes red in that case, then proves the real
  ``close_room`` (persist + reconcile) is green.
"""

from __future__ import annotations

import threading
import time as _time
import uuid

import pycrdt as Y
import pytest
from flask import Flask
from sqlalchemy import create_engine, select

import scribble
from scribble.api import api_bp
from scribble.blueprint import bp
from scribble.collab import crdt, pm_yjs
from scribble.content.render_html import render_block
from scribble.models import CollabDoc, Engagement, EngagementFinding
from scribble.seed import seed_defaults

BLOCK = "description"

# A fixture doc exercising every node type in the frozen content schema: marks, a variable chip, a
# nested bullet list, a heading, a figure/inlineImage, a code block, and a blockquote.
SAMPLE_DOC = {
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Affected host: ", "marks": [{"type": "bold"}]},
                {"type": "variable", "attrs": {"key": "TARGET_HOST"}},
                {"type": "hardBreak"},
                {"type": "text", "text": "link", "marks": [{"type": "link", "attrs": {"href": "https://x"}}]},
            ],
        },
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}],
                },
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "two"}]}],
                },
            ],
        },
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Heading"}]},
        {
            "type": "figure",
            "attrs": {"caption": "a screenshot"},
            "content": [{"type": "inlineImage", "attrs": {"artifactId": 7, "alt": "x"}}],
        },
        {"type": "codeBlock", "content": [{"type": "text", "text": "print(1)"}]},
        {
            "type": "blockquote",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "quoted"}]}],
        },
    ],
}


# Wire the collab websocket + status routes onto the shared blueprint objects the same way the driver
# will (before any scribble.register() call registers `bp`/`api_bp` on a Flask app — Flask forbids
# `.route()` after a blueprint has been registered once). Unlike the already-integrated WS4 hooks
# (autosave/presence, wired into `scribble/__init__.py:_wire_feature_routes` by the driver, which
# guarantees they run before the *first* `scribble.register()` call of the whole test session no matter
# which test file happens to run first), `crdt.register` isn't wired in yet — integrating it is the
# driver's job (see this workstream's rails: do not edit `scribble/__init__.py`). So this module-level
# call (executed once at import/collection time, before *any* test in the suite runs and thus before
# `bp`/`api_bp` can have been registered on an app) is what stands in for that until integration.
crdt.register(api_bp, bp)


# --------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def app(tmp_path):
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp_path), base_template="scribble/base.html"
    )
    with cfg.session_factory() as session:
        seed_defaults(session)
        session.commit()
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(app):
    return app.extensions["scribble"].session_factory


@pytest.fixture
def finding_id(session_factory) -> int:
    with session_factory() as db:
        engagement = Engagement(name="Collab Test", company_name="Acme")
        finding = EngagementFinding(
            engagement=engagement, title="Collab finding", content_json={}, content_html={}
        )
        db.add(engagement)
        db.add(finding)
        db.commit()
        return finding.id


@pytest.fixture(autouse=True)
def _isolated_manager(monkeypatch):
    """``scribble.collab.crdt.manager`` is a module-level singleton (mirrors
    ``scribble.collab.presence.registry``); swap in a fresh one per test so rooms from one test never
    leak into another. Only the register()/HTTP-status test below actually depends on this -- the rest
    instantiate their own ``RoomManager()`` directly."""
    fresh = crdt.RoomManager()
    monkeypatch.setattr(crdt, "manager", fresh)
    yield fresh


def _client_apply(ydoc: Y.Doc, message: bytes) -> bytes | None:
    """Simulate a remote client processing one message from the server (what a real Yjs provider's
    message handler would do)."""
    if not message:
        return None
    msg_type, payload = message[0], message[1:]
    if msg_type == Y.YMessageType.SYNC:
        return Y.handle_sync_message(payload, ydoc)
    return None


def _deliver(outbox: dict[str, list[bytes]], clients: dict[str, Y.Doc], manager, finding_id, block) -> None:
    """Route a Room.receive()/connect() outbox to the right simulated client docs, recursively feeding
    any reply a client generates (e.g. a SYNC_STEP2 answer to a SYNC_STEP1) back into the room."""
    for conn_id, messages in outbox.items():
        ydoc = clients.get(conn_id)
        if ydoc is None:
            continue
        for message in messages:
            reply = _client_apply(ydoc, message)
            if reply is not None:
                more = manager.receive(finding_id, block, conn_id, reply)
                _deliver(more, clients, manager, finding_id, block)


def _client_join(manager, finding_id, block, conn_id, client_doc, clients, session_factory) -> None:
    """Simulate a real Yjs provider's connect sequence: the server sends its own SYNC_STEP1 (handled by
    ``manager.connect``), and — symmetrically — the client sends *its own* SYNC_STEP1 so the server (and
    thus other peers) learn what this client already has and can send back what it's missing. Skipping
    this second half is a common protocol mistake: a bare STEP1 only carries a state *vector*, never
    content, so without the client also announcing itself, it would never receive the server's data."""
    with session_factory() as session:
        initial = manager.connect(session, finding_id, block, conn_id)
    _deliver({conn_id: initial}, clients, manager, finding_id, block)
    own_step1 = Y.create_sync_message(client_doc)
    outbox = manager.receive(finding_id, block, conn_id, own_step1)
    _deliver(outbox, clients, manager, finding_id, block)


# --------------------------------------------------------------------------------------- pm_yjs mapping


def _round_trip(doc: dict) -> dict:
    ydoc = Y.Doc()
    pm_yjs.doc_to_ydoc(doc, ydoc)
    return pm_yjs.ydoc_to_doc(ydoc)


def test_pm_yjs_round_trip_preserves_structure():
    assert _round_trip(SAMPLE_DOC) == SAMPLE_DOC


def test_round_trip_empty_doc():
    assert _round_trip({"type": "doc", "content": []}) == {"type": "doc", "content": []}


def test_round_trip_empty_paragraph_does_not_churn():
    """N1: an empty paragraph must round-trip to itself (content stays ``[]``), so opening+closing a
    session with zero edits doesn't rewrite content_json."""
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": []}]}
    assert _round_trip(doc) == doc


def test_round_trip_block_level_leaf_gets_no_spurious_content():
    """N1: a block-level leaf node (image / hardBreak / variable / inlineImage) must NOT gain a spurious
    ``content: []`` on the round trip."""
    for leaf in (
        {"type": "image", "attrs": {"src": "x.png", "alt": "x"}},
        {"type": "hardBreak"},
        {"type": "variable", "attrs": {"key": "TARGET_HOST"}},
        {"type": "inlineImage", "attrs": {"artifactId": 3, "alt": "shot"}},
    ):
        doc = {"type": "doc", "content": [leaf]}
        result = _round_trip(doc)
        assert result == doc, f"leaf {leaf['type']} churned on round trip: {result}"
        assert "content" not in result["content"][0]


def test_round_trip_adjacent_same_mark_text_coalesces_and_is_idempotent():
    """Adjacent text runs with identical marks coalesce (documented normalization, same as editor.js's
    mergeAdjacentText). It must be idempotent: a second round trip is a fixed point (no further churn)."""
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "foo", "marks": [{"type": "bold"}]},
                    {"type": "text", "text": "bar", "marks": [{"type": "bold"}]},
                ],
            }
        ],
    }
    once = _round_trip(doc)
    coalesced_run = {"type": "text", "text": "foobar", "marks": [{"type": "bold"}]}
    assert once == {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [coalesced_run]}],
    }
    assert _round_trip(once) == once  # idempotent: no ongoing churn on repeated sessions


def test_normalize_doc_is_idempotent_for_sample():
    once = pm_yjs.normalize_doc(SAMPLE_DOC)
    assert once == SAMPLE_DOC
    assert pm_yjs.normalize_doc(once) == once


# --------------------------------------------------------------------------------------- convergence


def test_two_clients_concurrent_edits_merge_for_real(session_factory, finding_id):
    """Two independent pycrdt.Doc 'clients' connect to the same room, each performs a *different*,
    concurrent (not sequenced) edit, and after exchanging updates through the real Room/RoomManager
    protocol, both clients AND the server room converge to a document containing BOTH edits. A
    last-writer-wins scheme (what Phase A's autosave does) would have silently dropped one."""
    manager = crdt.RoomManager()
    client_a, client_b = Y.Doc(), Y.Doc()
    clients = {"conn-a": client_a, "conn-b": client_b}

    # conn-a connects to an empty room and creates the baseline paragraph.
    _client_join(manager, finding_id, BLOCK, "conn-a", client_a, clients, session_factory)

    frag_a = client_a.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    state0 = client_a.get_state()
    with client_a.transaction():
        pm_yjs.append_node(
            frag_a.children, {"type": "paragraph", "content": [{"type": "text", "text": "Hello "}]}
        )
    baseline_update = client_a.get_update(state0)
    manager.receive(finding_id, BLOCK, "conn-a", Y.create_update_message(baseline_update))

    # conn-b connects afterwards -- its handshake pulls in the baseline paragraph.
    _client_join(manager, finding_id, BLOCK, "conn-b", client_b, clients, session_factory)
    assert pm_yjs.ydoc_to_doc(client_a) == pm_yjs.ydoc_to_doc(client_b)

    # --- concurrent, independent edits: neither client has seen the other's change yet ---
    frag_a = client_a.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    para_a = next(iter(frag_a.children))
    text_a = next(iter(para_a.children))
    state_a = client_a.get_state()
    with client_a.transaction():
        text_a.insert(len("Hello "), "World", {})
    update_a = client_a.get_update(state_a)

    frag_b = client_b.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    state_b = client_b.get_state()
    with client_b.transaction():
        pm_yjs.append_node(
            frag_b.children, {"type": "paragraph", "content": [{"type": "text", "text": "Second para"}]}
        )
    update_b = client_b.get_update(state_b)

    outbox_a = manager.receive(finding_id, BLOCK, "conn-a", Y.create_update_message(update_a))
    outbox_b = manager.receive(finding_id, BLOCK, "conn-b", Y.create_update_message(update_b))
    _deliver(outbox_a, clients, manager, finding_id, BLOCK)
    _deliver(outbox_b, clients, manager, finding_id, BLOCK)

    room = manager.get_room(finding_id, BLOCK)
    final_a = pm_yjs.ydoc_to_doc(client_a)
    final_b = pm_yjs.ydoc_to_doc(client_b)
    final_server = room.render()
    assert final_a == final_b == final_server, "clients and server did not converge to the same document"

    paragraph_texts = [n["content"][0]["text"] for n in final_a["content"] if n["type"] == "paragraph"]
    assert any("World" in t for t in paragraph_texts), "conn-a's concurrent edit was lost"
    assert any(t == "Second para" for t in paragraph_texts), "conn-b's concurrent edit was lost"


def test_same_position_concurrent_edits_merge(session_factory, finding_id):
    """A sharper proof of real CRDT merge: both clients insert *different* text at the exact same
    position in the same shared paragraph, concurrently. Last-writer-wins would keep only one insertion;
    a real CRDT keeps both, in a deterministic order both replicas agree on."""
    manager = crdt.RoomManager()
    client_a, client_b = Y.Doc(), Y.Doc()
    clients = {"conn-a": client_a, "conn-b": client_b}

    _client_join(manager, finding_id, BLOCK, "conn-a", client_a, clients, session_factory)

    frag_a = client_a.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    state0 = client_a.get_state()
    with client_a.transaction():
        pm_yjs.append_node(
            frag_a.children, {"type": "paragraph", "content": [{"type": "text", "text": "Hello "}]}
        )
    manager.receive(
        finding_id, BLOCK, "conn-a", Y.create_update_message(client_a.get_update(state0))
    )

    _client_join(manager, finding_id, BLOCK, "conn-b", client_b, clients, session_factory)
    assert pm_yjs.ydoc_to_doc(client_a) == pm_yjs.ydoc_to_doc(client_b)

    insert_at = len("Hello ")

    text_a = next(iter(next(iter(client_a.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment).children)).children))
    state_a = client_a.get_state()
    with client_a.transaction():
        text_a.insert(insert_at, "Beautiful ", {})
    update_a = client_a.get_update(state_a)

    text_b = next(iter(next(iter(client_b.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment).children)).children))
    state_b = client_b.get_state()
    with client_b.transaction():
        text_b.insert(insert_at, "Wonderful ", {})
    update_b = client_b.get_update(state_b)

    outbox_a = manager.receive(finding_id, BLOCK, "conn-a", Y.create_update_message(update_a))
    outbox_b = manager.receive(finding_id, BLOCK, "conn-b", Y.create_update_message(update_b))
    _deliver(outbox_a, clients, manager, finding_id, BLOCK)
    _deliver(outbox_b, clients, manager, finding_id, BLOCK)

    final_a = pm_yjs.ydoc_to_doc(client_a)
    final_b = pm_yjs.ydoc_to_doc(client_b)
    assert final_a == final_b, "conflicting concurrent edits did not converge identically"

    merged_text = final_a["content"][0]["content"][0]["text"]
    assert "Beautiful" in merged_text, "conn-a's concurrent insert was lost (last-writer-wins?)"
    assert "Wonderful" in merged_text, "conn-b's concurrent insert was lost (last-writer-wins?)"


def test_receive_is_mutually_excluded_by_room_lock(session_factory, finding_id):
    """C1 guard, DETERMINISTIC (RAILS §4): ``Room.receive`` must run under ``Room._lock`` so it can never
    touch the shared, non-thread-safe ``pycrdt.Doc`` concurrently with another thread. Proven directly:
    while the test holds ``room._lock``, a concurrent ``receive()`` on another thread MUST block (not
    complete) until the lock is released, then complete and apply the edit.

    This fails the instant ``with self._lock:`` is removed from ``Room.receive`` (the concurrent receive
    would complete despite the held lock) -- a deterministic complement to the stochastic stress test
    below (whose Rust panic, being GIL-timing-dependent, can't be made to fire on every single run)."""
    manager = crdt.RoomManager()
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")
    room = manager.get_room(finding_id, BLOCK)

    # A real update from a synced client.
    cdoc = Y.Doc()
    for m in room.add_connection("conn-2"):
        _client_apply(cdoc, m)
    cfrag = cdoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    before = cdoc.get_state()
    with cdoc.transaction():
        pm_yjs.append_node(
            cfrag.children, {"type": "paragraph", "content": [{"type": "text", "text": "locked edit"}]}
        )
    update_msg = Y.create_update_message(cdoc.get_update(before))

    done = threading.Event()

    def do_receive() -> None:
        room.receive("conn-2", update_msg)
        done.set()

    with room._lock:
        worker = threading.Thread(target=do_receive)
        worker.start()
        # The concurrent receive must block on the held lock -- it must NOT finish while we hold it.
        assert not done.wait(timeout=0.5), "Room.receive ran without acquiring room._lock (C1 race!)"
    # Lock released -> receive proceeds and applies the edit.
    assert done.wait(timeout=2.0), "receive never completed after the lock was released"
    worker.join()
    texts = [
        n["content"][0]["text"]
        for n in room.render()["content"]
        if n["type"] == "paragraph" and n.get("content")
    ]
    assert "locked edit" in texts


def test_many_threads_hammering_one_room_is_safe_and_converges(session_factory, finding_id):
    """C1 stress test (RAILS §4): flask-sock is one OS thread per socket, all sharing one room's
    pycrdt.Doc. Without ``Room._lock`` this crashes -- concurrent transactions on a single Doc raise a
    Rust ``PanicException`` ('Transaction is unsendable') / ``RuntimeError('Already in a transaction')``
    (proven empirically; ``allow_multithreading=True`` alone does NOT prevent it). With the per-room
    lock, N threads hammering the same room serialize safely and every contribution survives (real
    merge). WITH the lock this is deterministic-green (serialized); WITHOUT it, it crashes on the large
    majority of runs (the exact interleaving is GIL-timing-dependent — see the deterministic
    mutual-exclusion guard above for the airtight version)."""
    n_threads = 8
    edits_per_thread = 60
    payload = "x" * 4000  # widen each Doc transaction's window to surface the race without the lock
    clients: dict[str, Y.Doc] = {}
    manager = crdt.RoomManager()

    # Connect all clients first (single-threaded setup), each syncing to the (empty) room.
    with session_factory() as session:
        for i in range(n_threads):
            conn = f"conn-{i}"
            clients[conn] = Y.Doc()
            for m in manager.connect(session, finding_id, BLOCK, conn):
                _client_apply(clients[conn], m)

    # Hammer Room.receive directly on the shared room -- the exact method whose per-room lock is under
    # test. (Going through manager.receive would insert a get_room manager-lock acquire on every call,
    # serializing *entry* enough to further mask the Doc-level race.)
    room = manager.get_room(finding_id, BLOCK)
    errors: list[str] = []
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        conn = f"conn-{i}"
        cdoc = clients[conn]
        try:
            barrier.wait()  # maximize contention: everyone starts hammering at once
            for j in range(edits_per_thread):
                cfrag = cdoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
                before = cdoc.get_state()
                with cdoc.transaction():
                    pm_yjs.append_node(
                        cfrag.children,
                        {"type": "paragraph", "content": [{"type": "text", "text": f"t{i}-{j}-{payload}"}]},
                    )
                update = cdoc.get_update(before)
                # Concurrent calls into the SHARED room's Doc -- this is what the lock protects.
                room.receive(conn, Y.create_update_message(update))
        except BaseException as exc:  # noqa: BLE001 - capture Rust PanicException too
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent access to one room raised (missing/broken lock?): {errors[:3]}"

    final = room.render()
    texts = {
        n["content"][0]["text"]
        for n in final["content"]
        if n["type"] == "paragraph" and n.get("content")
    }
    expected = {f"t{i}-{j}-{payload}" for i in range(n_threads) for j in range(edits_per_thread)}
    assert expected <= texts, f"lost concurrent edits under contention ({len(expected - texts)} missing)"


# --------------------------------------------------------------------------------------- persistence


def test_persist_and_reload_recovers_prior_content(session_factory, finding_id):
    manager = crdt.RoomManager()
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")

    room = manager.get_room(finding_id, BLOCK)
    frag = room.ydoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    with room.ydoc.transaction():
        pm_yjs.append_node(
            frag.children, {"type": "paragraph", "content": [{"type": "text", "text": "Persisted content"}]}
        )

    with session_factory() as session:
        row = manager.persist(session, finding_id, BLOCK)
    assert row is not None and row.ydoc_state

    with session_factory() as session:
        stored = session.execute(
            select(CollabDoc).where(CollabDoc.finding_id == finding_id, CollabDoc.block == BLOCK)
        ).scalar_one()
        assert stored.ydoc_state == row.ydoc_state

    # Simulate the room being evicted (process restart / idle recycle) *without* going through
    # close_room, which would also reconcile -- this isolates persistence from reconciliation.
    manager._rooms.pop((finding_id, BLOCK), None)
    assert manager.get_room(finding_id, BLOCK) is None

    with session_factory() as session:
        reopened = manager.open_room(session, finding_id, BLOCK)
    reloaded = reopened.render()
    assert reloaded["content"][0]["content"][0]["text"] == "Persisted content"


def test_open_room_seeds_from_existing_content_json_when_never_persisted(session_factory, finding_id):
    """Opening live collab on a block that already has authored content (via Phase A autosave, say)
    should start the CRDT doc from that content, not blank it out."""
    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        finding.content_json = {BLOCK: SAMPLE_DOC}
        session.commit()

    manager = crdt.RoomManager()
    with session_factory() as session:
        room = manager.open_room(session, finding_id, BLOCK)
    assert room.render() == SAMPLE_DOC


def test_autosave_between_collab_sessions_survives_reopen(session_factory, finding_id):
    """C2 regression: Phase A autosave (always on) writes ``content_json`` but never ``ydoc_state``. If a
    collab session persisted ``ydoc_state``, then an autosave edited the block, reopening collab must NOT
    resurrect the stale persisted CRDT state and overwrite the autosave edit on close. ``content_json`` is
    authoritative -- the reopened room must re-seed from it.

    Fails before the fix (open_room would blindly apply the stale ydoc_state and ignore content_json)."""
    session1_doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "typed in session 1"}]}],
    }
    autosave_doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "edited by autosave"}]}],
    }

    manager = crdt.RoomManager()

    # --- collab session 1: type something, close (persists ydoc_state + reconciles content_json) ---
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")
    room = manager.get_room(finding_id, BLOCK)
    frag = room.ydoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    with room.ydoc.transaction():
        pm_yjs.append_node(frag.children, session1_doc["content"][0])
    with session_factory() as session:
        manager.close_room(session, finding_id, BLOCK)

    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        assert finding.content_json[BLOCK] == session1_doc  # reconciled
        stored = session.execute(
            select(CollabDoc).where(CollabDoc.finding_id == finding_id, CollabDoc.block == BLOCK)
        ).scalar_one()
        assert stored.ydoc_state  # persisted

    # --- Phase A autosave edits the block out-of-band (content_json only, never ydoc_state) ---
    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        finding.content_json = {**(finding.content_json or {}), BLOCK: autosave_doc}
        session.commit()

    # --- collab session 2: reopen -> must reflect the autosave edit, not the stale ydoc_state ---
    with session_factory() as session:
        room2 = manager.open_room(session, finding_id, BLOCK)
    assert room2.render() == autosave_doc, "reopened collab resurrected stale CRDT state over autosave"

    # --- and closing session 2 must not clobber the autosave content back to session 1's text ---
    with session_factory() as session:
        manager.close_room(session, finding_id, BLOCK)
    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        assert finding.content_json[BLOCK] == autosave_doc


def test_reopen_without_intervening_autosave_reuses_persisted_state(session_factory, finding_id):
    """The freshness check must NOT over-trigger: with no out-of-band edit, a reopen reuses the persisted
    ydoc_state (content unchanged, no spurious reseed/churn)."""
    doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "stable content"}]}],
    }
    manager = crdt.RoomManager()
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")
    room = manager.get_room(finding_id, BLOCK)
    frag = room.ydoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    with room.ydoc.transaction():
        pm_yjs.append_node(frag.children, doc["content"][0])
    with session_factory() as session:
        manager.close_room(session, finding_id, BLOCK)

    with session_factory() as session:
        reopened = manager.open_room(session, finding_id, BLOCK)
    assert reopened.render() == doc


# --------------------------------------------------------------------------------------- reconciliation


def test_close_room_reconciles_content_json_and_html(session_factory, finding_id):
    manager = crdt.RoomManager()
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")

    room = manager.get_room(finding_id, BLOCK)
    frag = room.ydoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    with room.ydoc.transaction():
        pm_yjs.append_node(
            frag.children,
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Reconciled: "},
                    {"type": "variable", "attrs": {"key": "TARGET_HOST"}},
                ],
            },
        )
    expected_doc = room.render()
    expected_html = render_block(expected_doc, artifact_url=crdt._artifact_url)

    with session_factory() as session:
        closed = manager.close_room(session, finding_id, BLOCK)
    assert closed is True
    assert manager.get_room(finding_id, BLOCK) is None  # evicted

    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        assert finding.content_json[BLOCK] == expected_doc
        assert finding.content_html[BLOCK] == expected_html


def test_persist_alone_does_not_reconcile_content(session_factory, finding_id):
    """Guard test (RAILS §4): ``persist()`` in isolation is exactly what remains if ``reconcile()`` were
    ever accidentally dropped from ``close_room`` during a refactor. This proves that specific regression
    would be caught -- the "content_json reflects the merged CRDT doc" assertion goes red under
    persist-only, and green once reconcile actually runs.

    N2: the block is pre-seeded with a DIFFERENT stale doc first, so this proves reconcile *updates* an
    existing value (overwrites the stale one), not merely populates an empty block."""
    stale_doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "STALE pre-existing"}]}],
    }
    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        finding.content_json = {BLOCK: stale_doc}
        session.commit()

    manager = crdt.RoomManager()
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")

    # open_room seeded the room from the stale doc; add MORE content so the merged doc differs from it.
    room = manager.get_room(finding_id, BLOCK)
    frag = room.ydoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    with room.ydoc.transaction():
        pm_yjs.append_node(
            frag.children, {"type": "paragraph", "content": [{"type": "text", "text": "Regression content"}]}
        )
    merged = room.render()
    assert merged != stale_doc  # sanity: the room content genuinely diverged from what's stored

    # The injected regression: persist the CRDT bytes but never reconcile.
    with session_factory() as session:
        manager.persist(session, finding_id, BLOCK)

    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        # Still the STALE doc -- persist didn't touch content_json.
        assert finding.content_json[BLOCK] == stale_doc
        with pytest.raises(AssertionError):
            assert finding.content_json.get(BLOCK) == merged  # RED under persist-only

    # The real, complete behavior: close_room persists AND *updates* content_json (stale -> merged).
    with session_factory() as session:
        manager.close_room(session, finding_id, BLOCK)

    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        assert finding.content_json[BLOCK] == merged  # GREEN: reconcile overwrote the stale value
        assert finding.content_json[BLOCK] != stale_doc
        assert finding.content_html[BLOCK] == render_block(merged, artifact_url=crdt._artifact_url)


# --------------------------------------------------------------------------------------- idle durability


def test_schedule_idle_persist_writes_after_debounce(session_factory, finding_id):
    manager = crdt.RoomManager(idle_persist_seconds=0.05)
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")

    room = manager.get_room(finding_id, BLOCK)
    frag = room.ydoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    with room.ydoc.transaction():
        pm_yjs.append_node(
            frag.children, {"type": "paragraph", "content": [{"type": "text", "text": "debounced"}]}
        )
    # Real edits flow through Room.receive(), which sets `dirty` (schedule_idle_persist's debounce is a
    # no-op for a clean room -- no point writing bytes that haven't changed). This test edits the ydoc
    # directly for setup convenience, bypassing receive(), so it must set the flag itself.
    room.dirty = True

    manager.schedule_idle_persist(finding_id, BLOCK, session_factory)

    deadline = _time.time() + 2.0
    row = None
    while _time.time() < deadline:
        with session_factory() as session:
            row = session.execute(
                select(CollabDoc).where(CollabDoc.finding_id == finding_id, CollabDoc.block == BLOCK)
            ).scalar_one_or_none()
        if row is not None and row.ydoc_state:
            break
        _time.sleep(0.02)
    assert row is not None and row.ydoc_state


def test_schedule_idle_persist_skips_when_room_not_dirty(session_factory, finding_id):
    """The debounce timer must not write bytes that haven't changed since the last persist."""
    manager = crdt.RoomManager(idle_persist_seconds=0.05)
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")

    room = manager.get_room(finding_id, BLOCK)
    assert room.dirty is False  # nothing has been received yet

    manager.schedule_idle_persist(finding_id, BLOCK, session_factory)
    _time.sleep(0.2)  # comfortably past the 0.05s debounce window

    with session_factory() as session:
        row = session.execute(
            select(CollabDoc).where(CollabDoc.finding_id == finding_id, CollabDoc.block == BLOCK)
        ).scalar_one_or_none()
    assert row is None, "persist() ran for a room with no unpersisted changes"


# --------------------------------------------------------------------------------------- register() hook


def test_register_is_idempotent_and_mounts_collab_status(client, finding_id):
    # register() already ran once at module import time (see the top of this file); call it again to
    # prove idempotency (must not raise -- Flask forbids adding routes to an already-registered
    # blueprint, so a non-guarded second call would blow up exactly like the full-suite run did before
    # this file switched to a module-level, collection-time registration call).
    crdt.register(api_bp, bp)

    resp = client.get(f"/scribble/api/findings/{finding_id}/blocks/{BLOCK}/collab-status")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "active": False, "connections": 0}


def test_collab_status_reflects_open_room(session_factory, client, finding_id):
    manager = crdt.manager  # the patched-fresh module singleton the mounted route actually uses
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")
        manager.connect(session, finding_id, BLOCK, "conn-2")

    resp = client.get(f"/scribble/api/findings/{finding_id}/blocks/{BLOCK}/collab-status")
    assert resp.get_json() == {"ok": True, "active": True, "connections": 2}

    with session_factory() as session:
        manager.close_room(session, finding_id, BLOCK)

    resp = client.get(f"/scribble/api/findings/{finding_id}/blocks/{BLOCK}/collab-status")
    assert resp.get_json() == {"ok": True, "active": False, "connections": 0}


# ----------------------------------------------------------------------------- W5: auth / validation / cap


def test_is_valid_block_rejects_oversized_and_bad_charset():
    assert crdt.is_valid_block("description")
    assert crdt.is_valid_block("custom_block-1.2")
    assert not crdt.is_valid_block("")
    assert not crdt.is_valid_block("x" * 65)  # > String(64)
    assert not crdt.is_valid_block("bad/slash")
    assert not crdt.is_valid_block("has space")
    assert not crdt.is_valid_block(None)


def test_authorize_connection_rejects_invalid_block(app):
    with app.app_context():
        assert crdt.authorize_connection(1, "description") is True
        assert crdt.authorize_connection(1, "x" * 65) is False


def test_authorize_connection_consults_host_hook(app):
    cfg = app.extensions["scribble"]
    cfg.extras["collab_authorize"] = lambda finding_id, block: block != "secret"
    try:
        with app.app_context():
            assert crdt.authorize_connection(1, "description") is True
            assert crdt.authorize_connection(1, "secret") is False
    finally:
        cfg.extras.pop("collab_authorize", None)


def test_authorize_connection_denies_when_hook_raises(app):
    cfg = app.extensions["scribble"]

    def boom(finding_id, block):
        raise RuntimeError("host authz backend down")

    cfg.extras["collab_authorize"] = boom
    try:
        with app.app_context():
            assert crdt.authorize_connection(1, "description") is False
    finally:
        cfg.extras.pop("collab_authorize", None)


def test_open_room_rejects_nonexistent_finding_and_creates_no_room(session_factory):
    """W5 memory-DoS guard: a room is never spun up for a finding id that doesn't exist."""
    manager = crdt.RoomManager()
    missing_id = uuid.uuid7()  # well-formed, absent — a UUID column cannot be asked about 999999
    with session_factory() as session:
        with pytest.raises(LookupError):
            manager.open_room(session, missing_id, BLOCK)
    assert manager.get_room(missing_id, BLOCK) is None
    assert not manager._rooms


def test_room_cap_is_enforced(session_factory, finding_id):
    """W5: the manager refuses to open more than ``max_rooms`` simultaneous rooms."""
    manager = crdt.RoomManager(max_rooms=1)
    with session_factory() as session:
        manager.connect(session, finding_id, "description", "conn-1")  # room #1
        with pytest.raises(RuntimeError):
            manager.open_room(session, finding_id, "remediation")  # would be room #2
    assert manager.get_room(finding_id, "remediation") is None


# --------------------------------------------------------------------------------------- W2: lifecycle races


def test_disconnect_only_evicts_when_last_connection_leaves(session_factory, finding_id):
    manager = crdt.RoomManager()
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")
        manager.connect(session, finding_id, BLOCK, "conn-2")

    with session_factory() as session:
        closed = manager.disconnect(session, finding_id, BLOCK, "conn-1")
    assert closed is False
    assert manager.get_room(finding_id, BLOCK) is not None  # conn-2 still here

    with session_factory() as session:
        closed = manager.disconnect(session, finding_id, BLOCK, "conn-2")
    assert closed is True
    assert manager.get_room(finding_id, BLOCK) is None  # now evicted (+ flushed)


def test_disconnect_flushes_and_reconciles_on_last_leave(session_factory, finding_id):
    manager = crdt.RoomManager()
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")
    room = manager.get_room(finding_id, BLOCK)
    frag = room.ydoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    with room.ydoc.transaction():
        pm_yjs.append_node(
            frag.children, {"type": "paragraph", "content": [{"type": "text", "text": "final content"}]}
        )
    expected = room.render()

    with session_factory() as session:
        assert manager.disconnect(session, finding_id, BLOCK, "conn-1") is True

    with session_factory() as session:
        finding = session.get(EngagementFinding, finding_id)
        assert finding.content_json[BLOCK] == expected  # reconciled on last-leave
        stored = session.execute(
            select(CollabDoc).where(CollabDoc.finding_id == finding_id, CollabDoc.block == BLOCK)
        ).scalar_one()
        assert stored.ydoc_state  # persisted on last-leave


def test_persist_updates_existing_row_never_duplicates(session_factory, finding_id):
    """W2: persist respects the unique ``(finding_id, block)`` constraint -- a pre-existing row is
    updated in place, never duplicated. (The ``except IntegrityError`` fallback in ``_persist_room``
    additionally guards the racy concurrent-insert case, which the manager lock already serializes away
    in-process; this deterministic test covers the observable no-duplicate property.)"""
    manager = crdt.RoomManager()
    with session_factory() as session:
        manager.connect(session, finding_id, BLOCK, "conn-1")
    room = manager.get_room(finding_id, BLOCK)
    frag = room.ydoc.get(pm_yjs.ROOT_KEY, type=Y.XmlFragment)
    with room.ydoc.transaction():
        pm_yjs.append_node(
            frag.children, {"type": "paragraph", "content": [{"type": "text", "text": "row exists"}]}
        )

    # Simulate a row already present for this (finding, block).
    with session_factory() as session:
        session.add(CollabDoc(finding_id=finding_id, block=BLOCK, ydoc_state=b"", updated_at_ms=1))
        session.commit()

    with session_factory() as session:
        row = manager.persist(session, finding_id, BLOCK)  # must not raise IntegrityError
    assert row is not None and row.ydoc_state

    with session_factory() as session:
        rows = session.execute(
            select(CollabDoc).where(CollabDoc.finding_id == finding_id, CollabDoc.block == BLOCK)
        ).scalars().all()
    assert len(rows) == 1  # updated in place, not duplicated
