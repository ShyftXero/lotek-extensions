"""Pastel data model.

``owner_id`` is a UUID soft-reference to a core ``User`` (no cross-schema FK). It MUST be
``sqlalchemy.Uuid`` — core v2 keys every surrogate PK on UUIDv7 and an ``Integer``/``String``
column is INV-INTEGRITY-03's exact red path (``cannot cast type uuid to integer``).

``slug`` is ``secrets.token_urlsafe(32)`` (256-bit CSPRNG), NOT a UUIDv7. UUIDv7 embeds a
millisecond timestamp — knowing when a paste was created shrinks the search space. The slug is the
only public credential; it must carry no structural information.

INV-TENANCY-06: a paste has no ``engagement_id``. Visibility rule: a paste is visible to its owner
and to anyone who holds its slug (subject to its access controls). That rule is declared and
enforced in exactly one place — ``pastel/service.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from pastel.db import Base, TimestampMixin, UuidPk

MAX_BODY = 500_000  # ~500 KB; server-side cap, not just a UI hint
MAX_TITLE = 200
SLUG_LENGTH = 64  # token_urlsafe(32) → 43 chars; 64 gives headroom


class Paste(Base, UuidPk, TimestampMixin):
    """One paste. Text only; slug is the public credential."""

    __tablename__ = "pastel_pastes"

    slug: Mapped[str] = mapped_column(String(SLUG_LENGTH), unique=True, index=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(MAX_TITLE), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")

    # Soft ref to core User (UUIDv7) — INV-INTEGRITY-03: Uuid, never Integer/String.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    owner_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Access controls (lotek-extensions#369)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    read_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ponytail: read_count is incremented atomically via a Core UPDATE; it can lag slightly under
    # extreme concurrency (two concurrent requests could both see count < limit and both serve),
    # but the UPDATE … RETURNING makes each increment globally ordered — no request double-serves
    # past the limit unless the DB itself races. Good enough for a paste service.
    read_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Werkzeug pbkdf2:sha256 hash string; NULL = no password required.
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Retirement (soft-delete — an expired/limit-hit paste is not removed, just blocked)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    retired_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
