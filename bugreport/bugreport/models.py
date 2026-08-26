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

from sqlalchemy import Enum, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

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
