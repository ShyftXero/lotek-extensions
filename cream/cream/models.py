"""CREAM data model — quotes + invoices, v2-native.

Every surrogate PK is a UUIDv7 (``UuidPk``). Cross-core references (``engagement_id``, ``client_id``) are
**UUID soft references** — the value is a core ``Engagement``/``Client`` id, but there is no cross-schema
FK (an extension must not own an FK into core, and CREAM may run standalone with no such tables). They are
UUID-typed, never Integer, because lotek keys those core rows on UUIDv7 — an Integer column could not hold
the value (the exact "cannot cast type uuid to integer" break the Vector alignment fixed).

**No authorization data lives here.** Tenancy is core's: ``engagement_id`` merely records which engagement
a document bills; the host's seam decides who may read it.

**Money is ``Decimal``** (:mod:`cream.money`) — ``Numeric(12, 2)`` columns annotated ``Mapped[Decimal]``,
never ``Mapped[float]``. See that module for why.

**Every column added after the first release must be NULLABLE or DEFAULTED.** ``db.create_all`` upgrades
an existing database by ``ALTER TABLE ... ADD COLUMN`` and *skips* any column that is neither — a NOT
NULL column with no default would simply never appear on a deployed instance, silently.

Tables are ``cream_``-prefixed so they never collide with host tables.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cream.db import Base, TimestampMixin, UuidPk
from cream.enums import DEFAULT_UNIT, DocKind, DocStatus
from cream.money import ZERO, money


class Brand(Base, UuidPk, TimestampMixin):
    """Who the document is *from* — the issuing firm's identity, logo, and house style.

    A singleton (``slot`` is unique and always ``"default"``): one CREAM install issues documents as one
    firm. Stored in the database rather than a config file because it is edited from the UI and because
    it must be **snapshotted into an issued document** — a firm that moves office next year cannot be
    allowed to rewrite the letterhead of an invoice already in a client's hands.
    """

    __tablename__ = "cream_brand"

    slot: Mapped[str] = mapped_column(String(16), unique=True, default="default")

    company_name: Mapped[str] = mapped_column(String(255), default="Your Firm")
    address: Mapped[str | None] = mapped_column(Text, nullable=True)          # multi-line, free text
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(64), nullable=True)     # EIN / VAT reg / ABN

    # A ``data:image/...;base64,`` URI, never a remote URL: the PDF renderer fetches whatever a document
    # references, so an http(s) logo would turn "render this invoice" into an outbound request from the
    # server — an SSRF primitive reachable by anyone who can edit branding. Enforced in `api._clean_logo`
    # AND again at render time; a bad value degrades to no logo, never to a fetch.
    logo_data_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    accent_color: Mapped[str] = mapped_column(String(32), default="#0f766e")
    font_stack: Mapped[str] = mapped_column(
        String(255), default="system-ui,-apple-system,'Segoe UI',Roboto,sans-serif"
    )

    default_currency: Mapped[str] = mapped_column(String(8), default="USD")
    default_tax_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_tax_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=ZERO)

    # Free text. CREAM processes no payments — this is how you tell the client where to send the money.
    payment_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    footer_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Default rules-of-engagement text pre-filled into a quote's authorization block.
    default_roe_terms: Mapped[str | None] = mapped_column(Text, nullable=True)


class NumberCounter(Base, UuidPk, TimestampMixin):
    """The issued-document sequence for one ``(kind, year)``.

    Replaces a MAX-scan over already-issued numbers, which raced: two concurrent issues both read the
    same maximum and both tried to claim ``…-0007``, colliding on the sparse-unique ``Document.number``.
    A dedicated row can be locked (``SELECT … FOR UPDATE``) for the duration of the issuing transaction.
    """

    __tablename__ = "cream_number_counters"
    __table_args__ = (UniqueConstraint("kind", "year", name="uq_cream_counter_kind_year"),)

    kind: Mapped[str] = mapped_column(String(16), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


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
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=ZERO)
    # Flat-rate vs hourly is data, not a branch: ("project", 1) vs ("hr", 16).
    default_unit: Mapped[str] = mapped_column(String(16), default=DEFAULT_UNIT)
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
    # The client's own reference (PO number, cost centre) — printed so their AP department can match it.
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # --- Bill-to: a SNAPSHOT, not a join -------------------------------------------------------------
    # Copied from the host's Client at creation and thereafter owned by the document. A client record
    # that gets corrected next quarter must not retroactively re-address an invoice already issued.
    bill_to_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bill_to_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    bill_to_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bill_to_attn: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Dates ---------------------------------------------------------------------------------------
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # The agreed testing window. A pentest quote that does not state when testing happens is not a
    # scoping document — and the same two dates are what the blue team gets told to expect.
    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- Adjustments ---------------------------------------------------------------------------------
    # Percentage OR fixed amount; the fixed amount wins when both are set (service.totals). Tax is a free
    # LABEL plus a rate, never a jurisdiction table: "VAT 20%", "AR Sales Tax 9.5%", "Reverse charge".
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=ZERO)
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    discount_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tax_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tax_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=ZERO)

    # --- Scope + authorization (quotes) --------------------------------------------------------------
    # The engagement's real targets, pulled from the host seam and frozen at issue. This is what makes a
    # signed quote the scope-of-record: the authorized ranges and the scanner's targets have one source.
    scope_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorization_required: Mapped[bool] = mapped_column(Boolean, default=False)
    signatory_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signatory_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    roe_terms: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The frozen document captured at issue — line items, totals, bill-to, issuer, scope. The immutable
    # record, and what an issued document RENDERS FROM (see cream.viewmodel), so the client copy stays
    # byte-identical even after branding or a rate card moves on.
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The rendered PDF in the host object store (engagement-attributed). UUID soft-ref, no FK.
    document_object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # An invoice created from an accepted quote points back at it. A quote is NEVER mutated into an
    # invoice in place — it stays frozen and this is the audit link between the two records.
    converted_from_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
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
    # The scoping prose under the one-line description — target ranges, what is and is not included.
    # Rendered through cream.markup's restricted subset (escaped first, then a few inline forms), so it
    # is safe in both the HTML preview and the PDF.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1.00"))
    unit: Mapped[str] = mapped_column(String(16), default=DEFAULT_UNIT)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=ZERO)
    source: Mapped[str] = mapped_column(String(128), default="manual")
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    document: Mapped[Document] = relationship(back_populates="line_items")

    @property
    def amount(self) -> Decimal:
        """``qty × unit_price``, exact, rounded half-up to the cent — never a float multiply."""
        return money(money(self.qty) * money(self.unit_price))
