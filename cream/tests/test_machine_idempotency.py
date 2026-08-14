"""The PAT machine API's retried-create protection (``cream/api_pat.py`` idempotency seam).

``create_document`` and ``add_item`` run their whole handler body through the host's injected
``extras['idempotent']`` seam. An agent that retries a create with the same ``Idempotency-Key`` (header
or body field) must get the ORIGINAL resource back — not a second draft / a duplicate line item. With no
key, or with a distinct key, each POST creates a new resource (the seam is a no-op then).

The host owns the seam; cream owns *whether the create-shaped routes are wrapped in it*. So these tests
inject a FAITHFUL in-memory replica of lotek's ``app.idempotency.make_idempotent`` (dedup on
``(principal_id, key)``, replay the stored ``(body, status)`` on a repeat) and prove cream's wrapping
actually dedups end-to-end. ``/sync`` is NOT wrapped (it writes nothing) and ``/issue``/``/void`` do not
exist (human-only), so neither can be — nothing to test there.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from flask import request

from cream.models import Document


def _make_fake_idempotent():
    """An in-memory stand-in for ``app.idempotency.make_idempotent``'s ``idempotent(principal, key,
    produce)`` seam, with the same contract the real one documents:

      * key falsy, or a principal with no usable ``.id`` -> NOT idempotent (run ``produce`` directly);
      * otherwise the first call for ``(principal_id, key)`` runs ``produce`` once and, ONLY IF it is a
        terminal success (2xx), stores its ``(body, status)`` together with a REQUEST FINGERPRINT
        (endpoint + path params + body hash); a repeat whose fingerprint MATCHES replays the stored 2xx
        WITHOUT re-running ``produce``, and a repeat whose fingerprint DIFFERS is answered **422** rather
        than silently replaying or silently re-executing. A non-2xx is NOT memoized — a retry re-executes
        once the condition clears. All of this mirrors ``app.idempotency`` after its hardening.
    """
    store: dict[tuple, tuple[dict, int]] = {}

    def _principal_id(principal):
        raw = getattr(principal, "id", principal)
        if raw is None:
            return None
        return raw if isinstance(raw, uuid.UUID) else str(raw)

    def idempotent(principal, key, produce):
        if not key:
            return produce()
        pid = _principal_id(principal)
        if pid is None:
            return produce()
        # The slot is (principal, key) — but a REQUEST FINGERPRINT is stored beside it and COMPARED,
        # exactly as `app.idempotency` does. This fake originally keyed on (principal, key) alone, which
        # made it promise something production does not: that a retry with a CHANGED body replays the
        # original. A test written against that passed here and would have failed mounted. A fake kinder
        # than production is worse than no fake — it certifies the wrong contract.
        slot = (pid, str(key))
        fingerprint = (
            request.endpoint or "",
            repr(sorted((request.view_args or {}).items(), key=lambda kv: kv[0])),
            hashlib.sha256(request.get_data() or b"").hexdigest(),
        )
        if slot in store:
            stored_fp, stored_response = store[slot]
            if stored_fp != fingerprint:
                # Same key, different request: a caller bug. 422, never a silent replay or re-execute.
                return ({"error": "unprocessable_entity",
                         "detail": "this Idempotency-Key was already used for a different request; "
                                   "use a new key for a new operation"}, 422)
            return stored_response
        body, status = produce()
        if 200 <= status < 300:  # only terminal successes are memoized (see docstring)
            store[slot] = (fingerprint, (body, status))
        return body, status

    idempotent.store = store  # exposed so a test can assert the slot was actually claimed
    return idempotent


@pytest.fixture
def idempotency(app):
    """Inject the fake seam into the mounted cream config the way lotek's ``_inject_host`` injects the
    real one (``cfg.extras['idempotent']``), and return it so a test can inspect its store."""
    seam = _make_fake_idempotent()
    app.extensions["cream"].extras["idempotent"] = seam
    return seam


MACHINE = "/cream/machine"


# ── create_document ──────────────────────────────────────────────────────────────────────────────────


def test_repeated_create_with_same_key_header_returns_the_original(pat_client, idempotency,
                                                                    engagement_id, session_factory):
    headers = {"Idempotency-Key": "create-abc"}
    first = pat_client.post(f"{MACHINE}/documents",
                            json={"engagement_id": str(engagement_id), "title": "Pentest"},
                            headers=headers)
    assert first.status_code == 201, first.get_data(as_text=True)
    id1 = first.get_json()["id"]

    # A TRUE retry — byte-identical body — replays the original verbatim without re-running produce().
    retry = pat_client.post(f"{MACHINE}/documents",
                            json={"engagement_id": str(engagement_id), "title": "Pentest"},
                            headers=headers)
    assert retry.status_code == 201
    assert retry.get_json()["id"] == id1

    # The same key with a CHANGED body is NOT a retry — it is the caller reusing a key for a different
    # request, and it is refused. Neither silent option is acceptable: replaying would hand back a
    # document whose title the caller did not ask for, and re-executing would mint the second draft the
    # key was sent to prevent.
    changed = pat_client.post(f"{MACHINE}/documents",
                              json={"engagement_id": str(engagement_id), "title": "Pentest (retry)"},
                              headers=headers)
    assert changed.status_code == 422, changed.get_data(as_text=True)

    with session_factory() as db:
        assert db.query(Document).count() == 1, "a retried create must not mint a second draft"
    assert (str(idempotency.store) != "{}")  # the slot was claimed


def test_repeated_create_with_body_idempotency_key_also_dedups(pat_client, idempotency,
                                                               engagement_id, session_factory):
    """The ``idempotency_key`` BODY field is the fallback for a client that can't set headers."""
    body = {"engagement_id": str(engagement_id), "title": "Q", "idempotency_key": "create-body-1"}
    first = pat_client.post(f"{MACHINE}/documents", json=body)
    second = pat_client.post(f"{MACHINE}/documents", json=body)
    assert first.status_code == second.status_code == 201
    assert first.get_json()["id"] == second.get_json()["id"]
    with session_factory() as db:
        assert db.query(Document).count() == 1


def test_no_key_creates_a_second_document(pat_client, idempotency, engagement_id, session_factory):
    """Control: without a key the seam is a no-op, so two POSTs create two distinct drafts."""
    a = pat_client.post(f"{MACHINE}/documents",
                        json={"engagement_id": str(engagement_id), "title": "One"})
    b = pat_client.post(f"{MACHINE}/documents",
                        json={"engagement_id": str(engagement_id), "title": "Two"})
    assert a.get_json()["id"] != b.get_json()["id"]
    with session_factory() as db:
        assert db.query(Document).count() == 2


def test_distinct_keys_create_distinct_documents(pat_client, idempotency, engagement_id, session_factory):
    """Control: a DIFFERENT key is a different operation, so it creates a new draft."""
    one = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "One"},
                          headers={"Idempotency-Key": "k1"})
    two = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Two"},
                          headers={"Idempotency-Key": "k2"})
    assert one.get_json()["id"] != two.get_json()["id"]
    with session_factory() as db:
        assert db.query(Document).count() == 2


def test_create_still_dedups_without_a_host_seam_is_not_claimed(pat_client, engagement_id,
                                                                session_factory):
    """When NO seam is injected (older host), the fallback runs ``produce`` directly — so a repeat with a
    key DOES create a second draft. This documents that dedup is an ENHANCEMENT gated on the host seam,
    never fails closed, and is exactly why the tests above inject one."""
    headers = {"Idempotency-Key": "no-seam"}
    a = pat_client.post(f"{MACHINE}/documents",
                        json={"engagement_id": str(engagement_id), "title": "A"}, headers=headers)
    b = pat_client.post(f"{MACHINE}/documents",
                        json={"engagement_id": str(engagement_id), "title": "B"}, headers=headers)
    assert a.status_code == b.status_code == 201
    assert a.get_json()["id"] != b.get_json()["id"]
    with session_factory() as db:
        assert db.query(Document).count() == 2


# ── add_item ─────────────────────────────────────────────────────────────────────────────────────────


def test_repeated_add_line_item_with_same_key_returns_the_original(pat_client, idempotency,
                                                                    engagement_id, session_factory):
    doc = pat_client.post(f"{MACHINE}/documents",
                          json={"engagement_id": str(engagement_id), "title": "Pentest"}).get_json()
    headers = {"Idempotency-Key": "line-1"}
    first = pat_client.post(f"{MACHINE}/documents/{doc['id']}/line-items",
                            json={"description": "External pentest", "qty": 5, "unit_price": 1500},
                            headers=headers)
    assert first.status_code == 201, first.get_data(as_text=True)
    line_id = first.get_json()["id"]

    second = pat_client.post(f"{MACHINE}/documents/{doc['id']}/line-items",
                             json={"description": "External pentest", "qty": 5, "unit_price": 1500},
                             headers=headers)
    assert second.status_code == 201
    assert second.get_json()["id"] == line_id  # the ORIGINAL line item, not an appended duplicate

    with session_factory() as db:
        stored = db.get(Document, uuid.UUID(doc["id"]))
        assert len(stored.line_items) == 1, "a retried add must not append a duplicate line item"
