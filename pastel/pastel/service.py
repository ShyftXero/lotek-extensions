"""Service layer: create, load, edit, delete, and the access-control READ gate.

Flask-free except for werkzeug's password hashing (a vendored dependency of Flask, not Flask itself), so
the whole access-control contract is unit-testable without a request.

The read gate (:func:`fetch_for_read`) is the security-critical part (#369). A PUBLIC GUID fetch runs,
in order:

  1. load by guid; unknown OR already retired -> None (caller renders 404)
  2. expiry: past ``expires_at`` -> retire(expired), None
  3. password: if set, the fetcher must present the right one -> else PasswordRequired
  4. read-count: atomic conditional increment; cap already hit -> retire(read_limit), None

Steps 2-4 fail CLOSED and are indistinguishable to the fetcher (a flat 404 — no existence oracle), with
ONE exception: a password-protected paste answers 401 so a legitimate reader can authenticate. ORDER
matters: a wrong password must NOT consume a read, so the password gate precedes the counting update.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from werkzeug.security import check_password_hash, generate_password_hash

from pastel.db import utcnow
from pastel.models import (
    CAUSE_EXPIRED,
    CAUSE_READ_LIMIT,
    MAX_BODY,
    MAX_TITLE,
    TTL_TOKENS,
    Paste,
    new_guid,
)

LIST_LIMIT = 200


class Denied(Exception):
    """Authorization refusal (an owner-only action attempted by a non-owner)."""


class Invalid(Exception):
    """A caller-supplied value is unusable; the message is written by this extension and safe to show."""


class PasswordRequired(Exception):
    """The paste is password-protected and the fetcher did not present the right password. Carries
    nothing — the caller turns it into a 401 Basic challenge."""


# --------------------------------------------------------------------------- time helpers


def _naive_utc(dt: datetime) -> datetime:
    """Coerce an aware datetime to naive UTC. Stored expiries are naive (DateTime columns); comparing a
    naive column value with an aware ``now`` raises TypeError, so both sides are normalised here. An aware
    input is first CONVERTED to UTC (not just stripped), so ``expires_at`` with a +05:00 offset is not
    silently shifted five hours — the leak window must mean what the caller said."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _now() -> datetime:
    return _naive_utc(utcnow())


# --------------------------------------------------------------------------- input cleaning


def resolve_ttl(expires_in, expires_at) -> datetime | None:
    """Turn an ``expires_in`` token (1h/6h/1d/7d/30d or integer seconds) OR an absolute ``expires_at``
    ISO-8601 string into a stored naive-UTC datetime, or None for 'never'. Raises Invalid otherwise.
    ``expires_at`` wins when both are given."""
    if expires_at not in (None, ""):
        try:
            dt = datetime.fromisoformat(str(expires_at))
        except (TypeError, ValueError) as exc:
            raise Invalid("expires_at must be an ISO-8601 timestamp") from exc
        return _naive_utc(dt)
    if expires_in in (None, "", "never"):
        return None
    token = str(expires_in).strip()
    if token in TTL_TOKENS:
        secs = TTL_TOKENS[token]
    else:
        try:
            secs = int(token)
        except (TypeError, ValueError) as exc:
            raise Invalid(f"expires_in must be one of {sorted(TTL_TOKENS)} or integer seconds") from exc
        if secs <= 0:
            raise Invalid("expires_in must be a positive number of seconds")
    return _now() + timedelta(seconds=secs)


def _clean_title(title) -> str | None:
    title = (title or "").strip()
    if len(title) > MAX_TITLE:
        raise Invalid("title too long")
    return title or None


def _clean_body(body) -> str:
    body = body if isinstance(body, str) else ("" if body is None else str(body))
    if len(body) > MAX_BODY:
        raise Invalid("paste body too large")
    if not body.strip():
        raise Invalid("paste body is empty")
    return body


def _clean_max_reads(max_reads) -> int | None:
    if max_reads in (None, "", 0, "0"):
        return None
    if isinstance(max_reads, bool):  # bool is an int subclass; a True max_reads is a bug, not "1 read"
        raise Invalid("max_reads must be a positive integer")
    try:
        n = int(max_reads)
    except (TypeError, ValueError) as exc:
        raise Invalid("max_reads must be a positive integer") from exc
    if n <= 0:
        raise Invalid("max_reads must be a positive integer")
    return n


def _clean_password(password) -> str | None:
    """A password is an opaque secret; only emptiness disqualifies it. Returns the stored HASH or None."""
    if password in (None, ""):
        return None
    return generate_password_hash(str(password))


# --------------------------------------------------------------------------- CRUD


def create(
    db,
    *,
    author_id,
    title,
    body,
    password=None,
    expires_in=None,
    expires_at=None,
    max_reads=None,
) -> Paste:
    """Create a paste. Raises Invalid on a bad body/title/ttl/max_reads. ``password`` is hashed here."""
    paste = Paste(
        guid=new_guid(),
        title=_clean_title(title),
        body=_clean_body(body),
        author_id=author_id,
        expires_at=resolve_ttl(expires_in, expires_at),
        max_reads=_clean_max_reads(max_reads),
        password_hash=_clean_password(password),
    )
    db.add(paste)
    db.commit()
    db.refresh(paste)
    return paste


def load_own(db, paste_id: uuid.UUID, *, author_id, is_admin) -> Paste | None:
    """A paste the caller OWNS (or any, for an admin). The authed, no-count owner path. None if not
    visible — 404, never 403 (no existence oracle)."""
    paste = db.get(Paste, paste_id)
    if paste is None:
        return None
    if is_admin:
        return paste
    if author_id is not None and paste.author_id == author_id:
        return paste
    return None


def visible_pastes(db, *, author_id, is_admin) -> list[Paste]:
    """The caller's own pastes (newest first), or every paste for an admin. Retired rows are still listed
    to their owner so they can see what happened and clean up."""
    stmt = select(Paste).order_by(Paste.created_at.desc()).limit(LIST_LIMIT)
    if not is_admin:
        if author_id is None:
            return []
        stmt = stmt.where(Paste.author_id == author_id)
    return list(db.scalars(stmt))


def update_own(db, paste: Paste, *, author_id, is_admin, title=..., body=...) -> Paste:
    """Owner/admin edit of the paste TEXT (title/body). Access-control settings are create-time only
    (see docs); editing them would reopen a retired or capped paste, which is not what an edit means.
    Sentinel defaults leave an unspecified field untouched."""
    if not (is_admin or (author_id is not None and paste.author_id == author_id)):
        raise Denied("not your paste")
    if title is not ...:
        paste.title = _clean_title(title)
    if body is not ...:
        paste.body = _clean_body(body)
    db.commit()
    db.refresh(paste)
    return paste


def adjust_access(
    db, paste: Paste, *, author_id, is_admin,
    add_reads=None, extend_days=None, new_password=None, clear_password=False, host_audit=None,
) -> Paste:
    """Owner/admin adjusts a paste's ACCESS controls after creation: add N reads (bumps the cap, and
    REOPENS a read-exhausted paste so it serves again), extend expiry to now+N days (reopens an expired
    one), set/change/clear the view password. Deliberately does what update_own refuses — the operator
    explicitly wants to reopen/extend. Audited ext:pastel:adjust_access. No-op fields are ignored."""
    if not (is_admin or (author_id is not None and paste.author_id == author_id)):
        raise Denied("not your paste")
    changed: dict = {}

    if add_reads is not None:
        if add_reads <= 0:
            raise Invalid("add_reads must be a positive integer")
        # Only a CAPPED paste gains reads; an uncapped one is already unlimited.
        if paste.max_reads is not None:
            paste.max_reads = paste.max_reads + add_reads
            changed["add_reads"] = add_reads
            # reopen if it was retired for hitting the cap and now has headroom
            if paste.retired_cause == CAUSE_READ_LIMIT and paste.read_count < paste.max_reads:
                paste.retired_at = None
                paste.retired_cause = None

    if extend_days is not None:
        if extend_days <= 0:
            raise Invalid("extend_days must be a positive integer")
        paste.expires_at = _now() + timedelta(days=extend_days)   # from NOW, per the operator
        changed["extend_days"] = extend_days
        if paste.retired_cause == CAUSE_EXPIRED:
            paste.retired_at = None
            paste.retired_cause = None

    if clear_password:
        paste.password_hash = None
        changed["password"] = "cleared"
    elif new_password:
        paste.password_hash = generate_password_hash(str(new_password))
        changed["password"] = "set"

    if not changed:
        raise Invalid("nothing to change")
    if host_audit is not None:
        host_audit(
            db, "ext:pastel:adjust_access", subject_type="pastel_paste",
            subject_id=str(paste.id), before=None, after=changed,
        )
    db.commit()
    db.refresh(paste)
    return paste


def delete_paste(db, paste: Paste, *, author_id, is_admin, host_audit=None) -> None:
    """Explicit owner/admin delete — a real DELETE (not a tombstone). Audited as ``delete`` with the
    guid but no content."""
    if not (is_admin or (author_id is not None and paste.author_id == author_id)):
        raise Denied("not your paste")
    if host_audit is not None:
        host_audit(
            db, "ext:pastel:delete", subject_type="pastel_paste", subject_id=str(paste.id),
            before={"guid": paste.guid}, after=None,
        )
    db.delete(paste)
    db.commit()


# --------------------------------------------------------------------------- retirement + the read gate


def _retire(db, paste: Paste, cause: str, *, host_audit=None) -> None:
    """Mark a paste retired (idempotent). Audited as ``retire`` with the cause only, never content."""
    if paste.retired_at is not None:
        return
    paste.retired_at = _now()
    paste.retired_cause = cause
    if host_audit is not None:
        host_audit(
            db, "ext:pastel:retire", subject_type="pastel_paste", subject_id=str(paste.id),
            before=None, after={"cause": cause},
        )
    db.commit()


def fetch_for_read(db, guid: str, *, password=None, host_audit=None) -> Paste | None:
    """Resolve a PUBLIC GUID fetch to a servable paste, applying every access gate, or raise
    PasswordRequired, or return None (caller -> 404). This is the ONLY path that counts a read."""
    paste = db.scalar(select(Paste).where(Paste.guid == guid))
    if paste is None or paste.retired_at is not None:
        return None

    # 1. expiry — lazy check-on-read.
    if paste.expires_at is not None and _now() >= _naive_utc(paste.expires_at):
        _retire(db, paste, CAUSE_EXPIRED, host_audit=host_audit)
        return None

    # 2. password — BEFORE counting, so a wrong/absent password never consumes a read.
    if paste.password_hash is not None:
        if not password or not check_password_hash(paste.password_hash, password):
            raise PasswordRequired()

    # 3. read-count — atomic conditional increment. Concurrent fetches cannot both pass the cap: the DB
    #    serialises this UPDATE, and rowcount tells us whether WE got the slot (no TOCTOU double-serve).
    if paste.max_reads is not None:
        res = db.execute(
            update(Paste)
            .where(
                Paste.id == paste.id,
                Paste.retired_at.is_(None),
                Paste.read_count < Paste.max_reads,
            )
            .values(read_count=Paste.read_count + 1)
        )
        if res.rowcount != 1:
            # Someone else took the last slot between our load and here.
            _retire(db, paste, CAUSE_READ_LIMIT, host_audit=host_audit)
            return None
        db.commit()
        db.refresh(paste)
        if paste.read_count >= paste.max_reads:
            # This fetch consumed the FINAL slot: serve it, then retire so the next fetch is a 404.
            _retire(db, paste, CAUSE_READ_LIMIT, host_audit=host_audit)

    return paste


def sweep_expired(db, *, host_audit=None) -> int:
    """Retire every live paste whose expiry has passed; returns the count retired. A convenience for a
    periodic job — correctness does NOT depend on it (fetch_for_read checks expiry lazily), so it is
    safe to never call. ponytail: O(n) scan over live-with-expiry rows; add an index-backed batch if a
    deployment ever accumulates enough expiring pastes for this to matter."""
    now = _now()
    rows = db.scalars(
        select(Paste).where(
            Paste.retired_at.is_(None),
            Paste.expires_at.is_not(None),
            Paste.expires_at <= now,
        )
    ).all()
    for paste in rows:
        _retire(db, paste, CAUSE_EXPIRED, host_audit=host_audit)
    return len(rows)


# --------------------------------------------------------------------------- serialization


def to_dict(paste: Paste, *, url: str | None = None, raw_url: str | None = None) -> dict:
    """Machine-API view of a paste. Never includes the body here (that is a content fetch) and never the
    password hash."""
    return {
        "id": str(paste.id),
        "guid": paste.guid,
        "title": paste.title,
        "url": url,
        "raw_url": raw_url,
        "created_at": paste.created_at.isoformat() if paste.created_at else None,
        "expires_at": paste.expires_at.isoformat() if paste.expires_at else None,
        "max_reads": paste.max_reads,
        "read_count": paste.read_count,
        "password_protected": paste.password_hash is not None,
        "retired": paste.retired_at is not None,
        "retired_cause": paste.retired_cause,
    }
