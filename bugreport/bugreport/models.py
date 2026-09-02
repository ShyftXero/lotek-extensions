"""Bugreport data model — one table, text only.

``reporter_id`` is a **UUID soft reference** to a core ``User`` (no cross-schema FK; the core table is
not known until mount time, and Bugreport may run standalone). It is ``sqlalchemy.Uuid``, never
``Integer``/``String``: core v2 keys every surrogate PK on UUIDv7, and an Integer core-ref column is
INV-INTEGRITY-03's exact red path (``cannot cast type uuid to integer`` — it has taken production down).

``reporter_name`` is denormalised on purpose so a report stays attributable after the account it came
from is gone.

A report holds NO client engagement data, so it has no ``engagement_id``. INV-TENANCY-06 allows that
only for a row gated by an *explicit declared platform rule* rather than default visibility — the rule
is declared, and enforced in exactly one place, in ``bugreport/service.py``: **a report is visible to
its reporter and to an admin, and to nobody else, on any surface.**
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bugreport.db import Base, TimestampMixin, UuidPk


class ReportStatus(enum.Enum):
    """Where a report stands. Everything past ``open`` is an ADMIN action, and is the feedback the
    reporter reads (#112: "admin acknowledged or deleted your report")."""

    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    # An admin "delete" is a TOMBSTONE, not a row removal: a deleted row cannot tell its reporter it was
    # deleted, which is the one thing #112 asks for. A reporter deleting their OWN report is a real
    # DELETE — there is nobody left to notify.
    deleted = "deleted"


#: Bound the two text fields server-side. Text-only capture is not a licence for unbounded storage, and
#: the browser `maxlength` is a hint, not a control.
MAX_TITLE = 200
MAX_BODY = 20_000


class Report(Base, UuidPk, TimestampMixin):
    """One filed bug report. Text only — no attachments, no links out, no outward filing."""

    __tablename__ = "bugreport_reports"

    # Soft ref to a core User (UUIDv7). NULL only in standalone mode, where there is no host identity;
    # mounted, a report with no resolvable reporter is refused rather than written unattributable.
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    reporter_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title: Mapped[str] = mapped_column(String(MAX_TITLE))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus), default=ReportStatus.open, index=True
    )
    # What the admin said about it — the feedback sentence the reporter reads.
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `selectin`, not the default lazy load: the list page renders many reports, and a per-report query
    # would be an N+1 bounded only by LIST_LIMIT. This fetches every report's attachments in ONE extra
    # statement. `delete-orphan` plus the FK's ON DELETE CASCADE means a reporter deleting their own
    # report takes its attachment ROWS with it — the BYTES are removed separately, by the service,
    # because the object store is not in the transaction.
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="report", cascade="all, delete-orphan", lazy="selectin",
        order_by="Attachment.created_at",
    )


#: Bound one upload. 25 MiB is generous for a screenshot or a log and small enough that a single
#: request cannot be used to fill the bucket. Enforced server-side while STREAMING, never by trusting
#: `Content-Length`, which the client also controls.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

#: Bound how many a single report can carry, so the per-file cap cannot be sidestepped by volume.
MAX_ATTACHMENTS_PER_REPORT = 20

#: The ONLY content types ever served inline, and every one is a raster image that browsers do not
#: execute. `image/svg+xml` is deliberately ABSENT: SVG is a script-capable document, and serving one
#: inline from lotek's own origin would run attacker JavaScript with the viewer's session — the whole
#: reason downloads default to `attachment` below. A claimed type is only honoured if the file's MAGIC
#: BYTES agree with it (see `service._sniff`), because the claim comes from the uploader.
INLINE_SAFE_TYPES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),  # plus a "WEBP" fourcc at offset 8 — see service._sniff
}

#: What everything else is served as. An opaque type plus `Content-Disposition: attachment` is how you
#: serve a file you did not write and cannot vouch for.
DEFAULT_CONTENT_TYPE = "application/octet-stream"


class Attachment(Base, UuidPk, TimestampMixin):
    """One file uploaded against a report. The BYTES live in the object store; this row is the metadata.

    **Tenancy.** An attachment has no engagement id for the same reason its report has none, and it is
    covered by the same explicit declared platform rule (INV-TENANCY-06): visibility is *inherited from
    the report*, resolved in one place — `service.load_attachment_visible` calls `load_visible` and adds
    nothing. There is deliberately no second predicate here; a copy is what drifts.

    **`share_token` is NOT a UUID, and that is the point.** This repo keys surrogate PKs on UUIDv7,
    which is a millisecond timestamp plus a monotonic counter — ordered, time-correlated, and a poor
    secret: knowing roughly when a file was uploaded shrinks the search space, and one known token
    tells you about its neighbours. A share link is a bearer capability, so it gets
    `secrets.token_urlsafe(32)` — 256 bits from the CSPRNG, no structure to exploit. The PK stays
    UUIDv7 per the repo convention; the two ids are separate on purpose and only the random one is ever
    published.

    NULL means **not shared**, which is the default. Sharing is an explicit act by the owner and is
    revocable by nulling this column (or rotating it, which invalidates every copy of the old link).
    """

    __tablename__ = "bugreport_attachments"

    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("bugreport_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Soft ref to a core User, same UUIDv7 rule as Report.reporter_id (INV-INTEGRITY-03).
    uploader_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # The uploader's original name, kept ONLY to put in Content-Disposition. Never used to build a
    # storage path: the object key is derived from this row's UUID PK, so a hostile filename has
    # nowhere to go.
    filename: Mapped[str] = mapped_column(String(255))
    # What we will SERVE it as — already reduced to INLINE_SAFE_TYPES or DEFAULT_CONTENT_TYPE at upload
    # time. The uploader's raw claim is never stored and never echoed.
    content_type: Mapped[str] = mapped_column(String(160), default=DEFAULT_CONTENT_TYPE)
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    # When the current share_token stops resolving (lotek#585). Stamped alongside the token in
    # `service.share_attachment`; a NULL here on a row that carries a token means a link minted before this
    # column existed — `_resolve_by_token` treats such legacy links as still valid (they were unbounded
    # when issued; re-sharing stamps an expiry). Nullable + additive so an existing DB migrates in place.
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    report: Mapped[Report] = relationship(back_populates="attachments")
