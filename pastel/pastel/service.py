"""Domain logic — Flask-free. All authorization flows through here.

Visibility rule (INV-TENANCY-06): a paste is visible to its owner and to anyone holding its
slug, subject to its access controls (expiry, read-count limit, password). This rule is declared
and enforced in one place: ``access_and_count``.

Security notes:
- Slug is 256-bit CSPRNG (never UUIDv7 — avoids time-ordered guessing).
- Password is Werkzeug pbkdf2:sha256 hash + constant-time compare.
- Read count is incremented atomically via a single UPDATE … RETURNING.
  Concurrent reads near the limit may both succeed (see the ponytail comment in models.py);
  the atomic UPDATE prevents any single request from double-serving.
- Expiry is checked on every read (lazy retirement); a background sweep is NOT required,
  though it may be added as a future improvement.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from sqlalchemy import select, update
from werkzeug.security import check_password_hash, generate_password_hash

from pastel.db import utcnow
from pastel.models import MAX_BODY, MAX_TITLE, Paste

LIST_LIMIT = 200
SLUG_BYTES = 32  # → 43 chars base64url; 256-bit unguessable


class NotFound(Exception):
    pass


class Gone(Exception):
    """Retired (expired/read-limit/deleted) — identical surface to NotFound (no existence oracle)."""


class Denied(Exception):
    pass


class PasswordRequired(Exception):
    pass


class WrongPassword(Exception):
    pass


class Invalid(Exception):
    pass


def _gen_slug() -> str:
    return secrets.token_urlsafe(SLUG_BYTES)


def create(
    db,
    *,
    owner_id: uuid.UUID | None,
    owner_name: str | None,
    body: str | None,
    title: str | None = None,
    expires_at: datetime | None = None,
    read_limit: int | None = None,
    password: str | None = None,
    standalone: bool = False,
    audit_fn=None,
) -> Paste:
    if not standalone and owner_id is None:
        raise Denied("a mounted paste must have an owner")
    if not body:
        raise Invalid("body is required")
    body = body.strip()
    if len(body) > MAX_BODY:
        raise Invalid(f"body exceeds {MAX_BODY // 1000} KB limit")
    title = (title or "").strip()[:MAX_TITLE] or None
    if read_limit is not None:
        try:
            read_limit = int(read_limit)
        except (TypeError, ValueError):
            raise Invalid("read_limit must be an integer") from None
        if read_limit < 1:
            raise Invalid("read_limit must be at least 1")

    paste = Paste(
        slug=_gen_slug(),
        body=body,
        title=title,
        owner_id=owner_id,
        owner_name=owner_name,
        expires_at=expires_at,
        read_limit=read_limit,
        read_count=0,
        password_hash=generate_password_hash(password) if password else None,
    )
    db.add(paste)
    db.flush()
    if audit_fn is not None:
        audit_fn(db, "ext:pastel:create", subject_type="paste", subject_id=paste.id,
                 before=None, after=to_dict(paste))
    db.commit()
    return paste


def load_by_slug(db, slug: str) -> Paste | None:
    return db.scalars(select(Paste).where(Paste.slug == slug)).first()


def _atomic_increment(db, paste_id: uuid.UUID) -> int | None:
    """Single-statement atomic increment. Returns the new read_count or None if the paste is gone."""
    result = db.execute(
        update(Paste)
        .where(Paste.id == paste_id, Paste.retired_at.is_(None))
        .values(read_count=Paste.read_count + 1)
        .returning(Paste.read_count)
    )
    row = result.one_or_none()
    db.commit()
    return row[0] if row is not None else None


def _do_retire(db, paste_id: uuid.UUID, *, reason: str, audit_fn=None) -> None:
    db.execute(
        update(Paste)
        .where(Paste.id == paste_id, Paste.retired_at.is_(None))
        .values(retired_at=utcnow(), retired_reason=reason)
    )
    if audit_fn is not None:
        audit_fn(db, "ext:pastel:retire", subject_type="paste", subject_id=paste_id,
                 before=None, after={"reason": reason})
    db.commit()


def access_and_count(
    db,
    paste: Paste,
    *,
    password: str | None = None,
    is_owner: bool = False,
    audit_fn=None,
) -> Paste:
    """Check access controls on an already-loaded paste; increment read count for non-owners.

    Raises NotFound/Gone/PasswordRequired/WrongPassword — callers map these to the appropriate
    HTTP response without revealing which condition triggered (no existence oracle).
    """
    now = utcnow()
    # Lazy expiry: retire on first read-attempt after deadline (issue #369)
    if paste.expires_at is not None and paste.expires_at <= now and paste.retired_at is None:
        _do_retire(db, paste.id, reason="expired", audit_fn=audit_fn)
        raise Gone()

    if paste.retired_at is not None:
        raise Gone()

    # Password gate (issue #369) — checked BEFORE read counting so wrong passwords do not count
    if paste.password_hash is not None:
        if password is None:
            raise PasswordRequired()
        if not check_password_hash(paste.password_hash, password):
            raise WrongPassword()

    # Read-count gate (issue #369) — owner previews do not count toward the limit
    if not is_owner:
        new_count = _atomic_increment(db, paste.id)
        if new_count is None:
            # Race: paste retired between our load and the update — fail safely
            raise Gone()
        if paste.read_limit is not None and new_count >= paste.read_limit:
            # This was the last allowed read; retire for future requests (this read still serves)
            _do_retire(db, paste.id, reason="read_limit", audit_fn=audit_fn)

    return paste


def load_for_read(
    db,
    slug: str,
    *,
    password: str | None = None,
    is_owner: bool = False,
    audit_fn=None,
) -> Paste:
    """Load a paste by slug and check access controls. Convenience wrapper over access_and_count."""
    paste = load_by_slug(db, slug)
    if paste is None:
        raise NotFound()
    return access_and_count(db, paste, password=password, is_owner=is_owner, audit_fn=audit_fn)


def delete_paste(
    db,
    slug: str,
    *,
    owner_id: uuid.UUID | None,
    is_admin: bool = False,
    audit_fn=None,
) -> Paste:
    paste = load_by_slug(db, slug)
    if paste is None:
        raise NotFound()
    if not is_admin and (owner_id is None or paste.owner_id != owner_id):
        raise Denied("only the owner may delete this paste")
    # Emit "delete" (not "retire") so admins can distinguish intentional deletions from
    # automatic expiry/read-limit retirements in the audit trail.
    db.execute(
        update(Paste)
        .where(Paste.id == paste.id, Paste.retired_at.is_(None))
        .values(retired_at=utcnow(), retired_reason="deleted")
    )
    if audit_fn is not None:
        audit_fn(db, "ext:pastel:delete", subject_type="paste", subject_id=paste.id,
                 before=to_dict(paste), after=None)
    db.commit()
    return paste


def list_own(db, owner_id: uuid.UUID | None, *, include_retired: bool = False) -> list[Paste]:
    stmt = select(Paste).where(Paste.owner_id == owner_id)
    if not include_retired:
        stmt = stmt.where(Paste.retired_at.is_(None))
    stmt = stmt.order_by(Paste.created_at.desc()).limit(LIST_LIMIT)
    return list(db.scalars(stmt).all())


def to_dict(paste: Paste) -> dict:
    return {
        "slug": paste.slug,
        "title": paste.title,
        "body": paste.body,
        "owner_id": str(paste.owner_id) if paste.owner_id else None,
        "owner_name": paste.owner_name,
        "expires_at": paste.expires_at.isoformat() if paste.expires_at else None,
        "read_limit": paste.read_limit,
        "read_count": paste.read_count,
        "has_password": paste.password_hash is not None,
        "retired_at": paste.retired_at.isoformat() if paste.retired_at else None,
        "retired_reason": paste.retired_reason,
        "created_at": paste.created_at.isoformat() if paste.created_at else None,
    }
