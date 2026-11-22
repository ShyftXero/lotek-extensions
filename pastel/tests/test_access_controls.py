"""The #369 access-control graders: expiry, read-count limit, password — and that they compose and fail
closed. These are the contract; a change that regresses one must break a test here.

A paste's content is fetched through the PUBLIC GUID surface (`/pastel/p/<guid>` and its `/raw`), which
is unauthenticated, so these tests drive the plain `client` with no session identity needed for a fetch.
Pastes are CREATED directly through the service so a test can pin exact field values (e.g. an expiry in
the past) without round-tripping the editor form.
"""

from __future__ import annotations

import base64
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import update

from pastel.db import utcnow
from pastel.models import CAUSE_EXPIRED, CAUSE_READ_LIMIT, Paste
from pastel.service import (
    PasswordRequired,
    create,
    fetch_for_read,
    sweep_expired,
)

PW_URL = "/pastel/p/{guid}"
RAW_URL = "/pastel/p/{guid}/raw"


def _mk(session_factory, **kw) -> str:
    """Create a paste through the service and return its guid."""
    kw.setdefault("author_id", uuid.uuid7())
    kw.setdefault("title", "t")
    kw.setdefault("body", "# hello\nbody text")
    with session_factory() as db:
        paste = create(db, **kw)
        return paste.guid


def _row(session_factory, guid) -> Paste:
    with session_factory() as db:
        return db.query(Paste).filter(Paste.guid == guid).one()


def _basic(password: str) -> dict:
    token = base64.b64encode(f"x:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --------------------------------------------------------------------------- baseline


def test_unknown_guid_is_404(client):
    assert client.get(PW_URL.format(guid="nope-not-a-real-guid")).status_code == 404


def test_plain_paste_renders_and_raw(client, session_factory):
    guid = _mk(session_factory, body="# Title\n\nbody")
    rendered = client.get(PW_URL.format(guid=guid))
    assert rendered.status_code == 200
    assert b"<h1" in rendered.data  # markdown rendered
    raw = client.get(RAW_URL.format(guid=guid))
    assert raw.status_code == 200
    assert raw.data == b"# Title\n\nbody"
    assert raw.mimetype == "text/plain"


# --------------------------------------------------------------------------- expiry


def test_expired_paste_is_404_and_retired(client, session_factory):
    guid = _mk(session_factory, expires_in="1h")
    # Shove the expiry into the past.
    with session_factory() as db:
        db.execute(
            update(Paste).where(Paste.guid == guid)
            .values(expires_at=utcnow().replace(tzinfo=None) - timedelta(minutes=1))
        )
        db.commit()
    assert client.get(PW_URL.format(guid=guid)).status_code == 404
    row = _row(session_factory, guid)
    assert row.retired_at is not None and row.retired_cause == CAUSE_EXPIRED


def test_future_expiry_still_serves(client, session_factory):
    guid = _mk(session_factory, expires_in="1d")
    assert client.get(PW_URL.format(guid=guid)).status_code == 200


def test_expires_at_offset_is_converted_to_utc_not_shifted(session_factory):
    """An aware `expires_at` must be CONVERTED to UTC, not have its tzinfo stripped — otherwise a +05:00
    offset would shift the leak window five hours. 00:00+05:00 == 19:00 UTC the previous day."""
    from pastel.service import resolve_ttl

    got = resolve_ttl(None, "2030-01-01T00:00:00+05:00")
    assert got is not None
    assert (got.year, got.month, got.day, got.hour) == (2029, 12, 31, 19)
    assert got.tzinfo is None  # stored naive-UTC


def test_sweep_expired_retires_due_rows(session_factory):
    live = _mk(session_factory, expires_in="1d")
    due = _mk(session_factory, expires_in="1h")
    with session_factory() as db:
        db.execute(
            update(Paste).where(Paste.guid == due)
            .values(expires_at=utcnow().replace(tzinfo=None) - timedelta(seconds=1))
        )
        db.commit()
        n = sweep_expired(db)
    assert n == 1
    assert _row(session_factory, due).retired_cause == CAUSE_EXPIRED
    assert _row(session_factory, live).retired_at is None


# --------------------------------------------------------------------------- read-count limit


def test_read_count_limit_exact(client, session_factory):
    guid = _mk(session_factory, max_reads=2)
    assert client.get(PW_URL.format(guid=guid)).status_code == 200
    assert client.get(PW_URL.format(guid=guid)).status_code == 200
    assert client.get(PW_URL.format(guid=guid)).status_code == 404  # 3rd read refused
    row = _row(session_factory, guid)
    assert row.read_count == 2
    assert row.retired_cause == CAUSE_READ_LIMIT


def test_read_count_one_is_single_use(client, session_factory):
    guid = _mk(session_factory, max_reads=1)
    assert client.get(PW_URL.format(guid=guid)).status_code == 200
    assert client.get(PW_URL.format(guid=guid)).status_code == 404


def test_raw_fetch_counts_as_a_read(client, session_factory):
    guid = _mk(session_factory, max_reads=1)
    assert client.get(RAW_URL.format(guid=guid)).status_code == 200
    assert client.get(RAW_URL.format(guid=guid)).status_code == 404
    assert client.get(PW_URL.format(guid=guid)).status_code == 404


def test_read_count_atomic_under_interleave(session_factory):
    """Two fetchers both load the row before either increments (the TOCTOU window). The conditional
    UPDATE + rowcount guard must let exactly ONE through for max_reads=1 — no double-serve."""
    guid = _mk(session_factory, max_reads=1)
    s1, s2 = session_factory(), session_factory()
    p1 = s1.query(Paste).filter(Paste.guid == guid).one()
    p2 = s2.query(Paste).filter(Paste.guid == guid).one()
    assert p1.read_count == 0 and p2.read_count == 0  # both saw a free slot

    def claim(db, paste) -> bool:
        res = db.execute(
            update(Paste).where(
                Paste.id == paste.id, Paste.retired_at.is_(None), Paste.read_count < Paste.max_reads
            ).values(read_count=Paste.read_count + 1)
        )
        db.commit()
        return res.rowcount == 1

    assert claim(s1, p1) is True    # first wins the slot
    assert claim(s2, p2) is False   # second is refused — the cap held
    s1.close()
    s2.close()
    assert _row(session_factory, guid).read_count == 1


# --------------------------------------------------------------------------- password


def test_password_challenges_without_credentials(client, session_factory):
    guid = _mk(session_factory, password="s3cret")
    resp = client.get(PW_URL.format(guid=guid))
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_password_wrong_is_401(client, session_factory):
    guid = _mk(session_factory, password="s3cret")
    assert client.get(PW_URL.format(guid=guid), headers=_basic("nope")).status_code == 401


def test_password_correct_serves(client, session_factory):
    guid = _mk(session_factory, password="s3cret")
    resp = client.get(PW_URL.format(guid=guid), headers=_basic("s3cret"))
    assert resp.status_code == 200


def test_password_applies_to_raw(client, session_factory):
    guid = _mk(session_factory, password="s3cret")
    assert client.get(RAW_URL.format(guid=guid)).status_code == 401
    assert client.get(RAW_URL.format(guid=guid), headers=_basic("s3cret")).status_code == 200


def test_wrong_password_does_not_consume_a_read(client, session_factory):
    """The password gate precedes the counting update, so failed auth never burns a read."""
    guid = _mk(session_factory, password="s3cret", max_reads=1)
    for _ in range(3):
        assert client.get(PW_URL.format(guid=guid), headers=_basic("wrong")).status_code == 401
    assert _row(session_factory, guid).read_count == 0  # not spent
    assert client.get(PW_URL.format(guid=guid), headers=_basic("s3cret")).status_code == 200
    assert client.get(PW_URL.format(guid=guid), headers=_basic("s3cret")).status_code == 404  # now spent


def test_password_hash_is_not_plaintext(session_factory):
    guid = _mk(session_factory, password="s3cret")
    row = _row(session_factory, guid)
    assert row.password_hash is not None
    assert "s3cret" not in row.password_hash


# --------------------------------------------------------------------------- composition / fail-closed


def test_all_three_compose_and_expiry_fails_closed(client, session_factory):
    """A paste that is password-protected AND read-limited AND expired is a 404 even with the right
    password — a failure on any one gate fails the whole fetch closed."""
    guid = _mk(session_factory, password="s3cret", max_reads=5, expires_in="1h")
    with session_factory() as db:
        db.execute(
            update(Paste).where(Paste.guid == guid)
            .values(expires_at=utcnow().replace(tzinfo=None) - timedelta(seconds=1))
        )
        db.commit()
    assert client.get(PW_URL.format(guid=guid), headers=_basic("s3cret")).status_code == 404
    assert _row(session_factory, guid).read_count == 0  # expiry gate ran before counting


def test_fetch_for_read_raises_password_required(session_factory):
    guid = _mk(session_factory, password="s3cret")
    with session_factory() as db:
        with pytest.raises(PasswordRequired):
            fetch_for_read(db, guid, password=None)


# --------------------------------------------------------------------------- owner preview does not count


def test_owner_preview_does_not_count_as_a_read(client, session_factory, hooks):
    """#369: key on the unguessable GUID fetch, not an authed owner view. The author's `/pastel/<id>`
    preview must not spend a read-limited paste."""
    with session_factory() as db:
        # Owned by the conftest session actor, so the authed owner-preview route resolves it.
        paste = create(db, author_id=hooks["actor"].id, title="t", body="x", max_reads=1)
        pid, guid = paste.id, paste.guid
    # Preview it twice through the authed owner route; the public read must still be available.
    assert client.get(f"/pastel/{pid}").status_code == 200
    assert client.get(f"/pastel/{pid}").status_code == 200
    assert _row(session_factory, guid).read_count == 0  # previews did not count
    assert client.get(PW_URL.format(guid=guid)).status_code == 200  # the one public read still works


# --------------------------------------------------------------------------- audit


def test_retirement_is_audited_with_cause_not_content(client, session_factory, audit_log):
    guid = _mk(session_factory, body="SECRET-BODY", max_reads=1)
    client.get(PW_URL.format(guid=guid))  # consumes the one read -> retire(read_limit)
    retire_events = [e for e in audit_log.events if e["action"] == "retire"]
    assert retire_events, "a retirement must be audited"
    ev = retire_events[-1]
    assert ev["after"] == {"cause": CAUSE_READ_LIMIT}
    assert "SECRET-BODY" not in repr(ev)  # never the content
