"""PASTEL data model — one table, ``pastel_pastes``.

A paste is markdown behind an unguessable GUID. The GUID (not the UUIDv7 PK) is what a reader's URL
carries; the PK is internal and never published. Three optional access gates hang off the same row —
expiry, a read-count limit, and a password — and they compose: a paste can be all three at once, and any
one failing fails the fetch CLOSED (see ``service.fetch_for_read``).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from pastel.db import Base, TimestampMixin, UuidPk

MAX_TITLE = 200
MAX_BODY = 512 * 1024  # 512 KiB of markdown source; a paste is text, not a file dump.
GUID_BYTES = 16        # secrets.token_urlsafe(16) -> a 128-bit, URL-safe, unguessable id.

# Relative-TTL tokens accepted at create time (editor + machine API), -> seconds.
TTL_TOKENS: dict[str, int] = {
    "1h": 3600,
    "6h": 6 * 3600,
    "1d": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}

# Retirement causes recorded on a retired row (the cause only — never content).
CAUSE_EXPIRED = "expired"
CAUSE_READ_LIMIT = "read_limit"
CAUSE_DELETED = "deleted"


def new_guid() -> str:
    return secrets.token_urlsafe(GUID_BYTES)


class Paste(UuidPk, TimestampMixin, Base):
    __tablename__ = "pastel_pastes"

    # The published, unguessable capability carried in the URL. Separate from the PK so the PK stays a
    # monotonic UUIDv7 while the thing a reader sees reveals no ordering or count.
    guid: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=new_guid)
    title: Mapped[str | None] = mapped_column(String(MAX_TITLE), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    # Soft ref to a core User (UUIDv7). Same INV-INTEGRITY-03 rule as bugreport.reporter_id: a core id is
    # Uuid, never Integer/String. NULL = authored standalone / anonymously.
    author_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)

    # --- access controls (#369); all optional, all compose, all fail closed ---
    # Stored naive-UTC (see service._now); compared naive-to-naive to avoid aware/naive TypeErrors.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_reads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # A werkzeug password hash (salted, constant-time verified). NULL = no password.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Retirement is terminal: a retired paste is 404 forever, exactly like an unknown GUID (no existence
    # oracle). ``retired_cause`` is for the audit trail only, never leaked to a fetcher.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_cause: Mapped[str | None] = mapped_column(String(32), nullable=True)
