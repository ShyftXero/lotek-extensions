"""CREAM data model — quotes + invoices, v2-native.

Every surrogate PK is a UUIDv7 (``UuidPk``). Cross-core references (``engagement_id``, ``client_id``) are
**UUID soft references** — the value is a core ``Engagement``/``Client`` id, but there is no cross-schema
FK (an extension must not own an FK into core, and CREAM may run standalone with no such tables). They are
UUID-typed, never Integer, because lotek keys those core rows on UUIDv7 — an Integer column could not hold
the value (the exact "cannot cast type uuid to integer" break the Vector alignment fixed).

**No authorization data lives here.** Tenancy is core's: ``engagement_id`` merely records which engagement
a document bills; the host's seam decides who may read it.

Tables are ``cream_``-prefixed so they never collide with host tables.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cream.db import Base, TimestampMixin, UuidPk
from cream.enums import DocKind, DocStatus


class RateCard(Base, UuidPk, TimestampMixin):
    """A named set of default prices. Everything is editable; a rate card only *suggests* line items."""

    __tablename__ = "cream_rate_cards"

    name: Mapped[str] = mapped_column(String(128), unique=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    items: Mapped[list[RateItem]] = relationship(
        back_populates="rate_card", cascade="all, delete-orphan"
    )


class RateItem(Base, UuidPk, TimestampMixin):
    """Maps a billable UNIT (a run-type / phase / scope band such as ``host-band:1-256``) to a default
    line-item label + unit price. When an engagement gains a not-yet-billed unit, the draft gets a
    SUGGESTED line item from here — accepted/edited/removed by a human."""

    __tablename__ = "cream_rate_items"

    rate_card_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cream_rate_cards.id", ondelete="CASCADE"), index=True
    )
    unit_key: Mapped[str] = mapped_column(String(128), index=True)  # e.g. "run_type:external_pentest"
    label: Mapped[str] = mapped_column(String(255))
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    rate_card: Mapped[RateCard] = relationship(back_populates="items")


class Document(Base, UuidPk, TimestampMixin):
    """A quote (SOW) or an invoice. A DRAFT tracks its engagement live; ISSUING freezes an immutable,
    numbered snapshot (client copy == your copy). No payment processing — collection is off-platform."""

    __tablename__ = "cream_documents"

    kind: Mapped[DocKind] = mapped_column(Enum(DocKind), default=DocKind.invoice, index=True)
    status: Mapped[DocStatus] = mapped_column(Enum(DocStatus), default=DocStatus.draft, index=True)
    # Tenancy key — NOT NULL (INV-TENANCY-06 / B2): every document bills exactly one engagement, and a
    # job-only doc derives it from JobDTO.engagement_id (itself NOT NULL). A soft UUID ref to a core
    # Engagement (no cross-schema FK); the host seam (can_operate_on / visible_engagement_ids) decides
    # who may read/write it — never a request body's engagement id.
    engagement_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # Assigned AT ISSUE (a draft has no number). Sparse-unique among issued docs.
    number: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(255), default="Untitled")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The frozen line items captured at issue — the immutable record even if the drafts later change.
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The rendered PDF in the host object store (engagement-attributed). UUID soft-ref, no FK.
    document_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Attribution (soft): who created it. Never authorization.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    line_items: Mapped[list[LineItem]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="LineItem.order_index"
    )


class LineItem(Base, UuidPk, TimestampMixin):
    """One priced line on a document. Everything editable. ``source`` records where it came from — a
    rate-card ``unit_key`` (auto-suggested from engagement growth) or ``manual`` (hand-authored)."""

    __tablename__ = "cream_line_items"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cream_documents.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(String(512))
    qty: Mapped[float] = mapped_column(Numeric(12, 2), default=1)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    source: Mapped[str] = mapped_column(String(128), default="manual")
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    document: Mapped[Document] = relationship(back_populates="line_items")

    @property
    def amount(self) -> float:
        return float(self.qty) * float(self.unit_price)
