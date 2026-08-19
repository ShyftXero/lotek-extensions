"""ext#47 — the client 404 on `POST /scribble/machine/engagements` must not dead-end the caller.

The reported sequence, from a real PAT-driven deliverable:

    POST /api/v1/clients                  -> 201  {"id": "<uuid>", ...}
    POST /scribble/machine/engagements    -> 404  {"error":"not_found","detail":"client not found"}

The caller holds an id it just created, and the refusal gives it nothing to act on. Both halves of the
behaviour are deliberate and neither changed here:

  * a core client is record-only (`api_v1.create_client_api` sets `owner_id` and nothing else); the
    membership that unlocks Scribble is minted by the first engagement under it
    (`POST /api/v1/engagements`, which self-grants an `operator` membership — and is ADMIN-ONLY);
  * `can_view_client_id` is membership-only on purpose, and its refusal is identical for "no such
    client" and "exists but you hold no grant" — no existence oracle over the client id space.

So the fix is the MESSAGE: a static next-step hint, appended unconditionally. These tests pin both
halves of that — that the hint is actually there, and that adding it did not turn the refusal into an
oracle. The second is the load-bearing one: the obvious "helpful" version of this fix (naming the id,
or saying *which* of the two cases applies) is exactly the leak the 404 exists to prevent.
"""

from __future__ import annotations

import uuid

import scribble.models as fm
from tests.conftest import StubActor

M = "/scribble/machine"

# lotek#335 -- Scribble's own `Client` table (the standalone/unmounted client model) is UUID-keyed too,
# so these are UUIDs, not the small ints this file used before the PK migration.
GRANTED = uuid.uuid7()      # a client the token under test holds a grant under
UNGRANTED = uuid.uuid7()    # a client that EXISTS (a real row, below) but which the token holds nothing under
NONEXISTENT = uuid.uuid7()  # an id with no row anywhere


def _token(stub_host) -> None:
    """A perfectly valid write-scoped token holding exactly one grant — the shape of the reporting
    session in ext#47, not an unauthenticated stranger."""
    stub_host.actor = StubActor(id=11, username="report-bot", role="operator")
    stub_host.viewable_client_ids = {GRANTED}


def _existing_ungranted_client(session_factory) -> None:
    """Make UNGRANTED a client that genuinely EXISTS.

    Without this row the byte-identity test below would be vacuous — both ids would be "nothing there"
    and the assertion would hold for a route that leaked the difference. With it, the two requests differ
    in the real world and must still be indistinguishable in the response.
    """
    with session_factory() as db:
        db.add(fm.Client(id=UNGRANTED, name="Someone else's client"))
        db.commit()


def test_a_granted_client_still_creates_an_engagement(client, stub_host):
    """Positive control. A refusal test proves nothing if the route refuses everything."""
    _token(stub_host)
    resp = client.post(f"{M}/engagements", json={"name": "Ours", "client_id": GRANTED})
    assert resp.status_code == 201, resp.get_json()


def test_client_not_found_names_the_next_step(client, stub_host, session_factory):
    """The refusal must tell an agent what to do next: the client is record-only, and the engagement
    create is what mints the membership (plus the fact that it is admin-only, or a non-admin caller
    dead-ends one step later)."""
    _existing_ungranted_client(session_factory)
    _token(stub_host)
    resp = client.post(f"{M}/engagements", json={"name": "Theirs", "client_id": UNGRANTED})

    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"] == "not_found"
    detail = body["detail"]
    assert "record-only" in detail
    assert "POST /api/v1/engagements" in detail
    assert "admin-only" in detail


def test_client_refusal_is_byte_identical_for_missing_and_ungranted(client, stub_host, session_factory):
    """No existence oracle: a client that EXISTS but is outside the token's grants, and an id with no
    row at all, must be the same answer byte for byte — otherwise the client id space is enumerable by
    diffing refusals, which is the whole reason this is a 404 and not a 403.

    Byte-level, not `.get_json()`: key order and whitespace are observable too.
    """
    _existing_ungranted_client(session_factory)
    _token(stub_host)

    ungranted = client.post(f"{M}/engagements", json={"name": "E", "client_id": UNGRANTED})
    missing = client.post(f"{M}/engagements", json={"name": "E", "client_id": NONEXISTENT})

    assert ungranted.status_code == missing.status_code == 404
    assert ungranted.data == missing.data, (
        "the two client refusals differ — that is an existence oracle over the client id space: "
        f"{ungranted.data!r} vs {missing.data!r}"
    )


def test_neither_refusal_created_anything(client, stub_host, session_factory):
    """A refused create must leave no row behind — the hint is a message change, not a widening."""
    _existing_ungranted_client(session_factory)
    _token(stub_host)
    client.post(f"{M}/engagements", json={"name": "E", "client_id": UNGRANTED})
    client.post(f"{M}/engagements", json={"name": "E", "client_id": NONEXISTENT})
    with session_factory() as db:
        assert db.query(fm.Engagement).count() == 0


def test_the_engagement_refusal_is_untouched(client, stub_host, session_factory):
    """The client hint must not bleed into the ENGAGEMENT 404 (`_engagement_not_found`), which is a
    different refusal about a different id space and already says all it should."""
    _token(stub_host)
    with session_factory() as db:
        eng = fm.Engagement(name="Theirs", client_id=UNGRANTED)
        db.add(eng)
        db.commit()
        eid = eng.id

    resp = client.get(f"{M}/engagements/{eid}")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not_found", "detail": "engagement not found"}
