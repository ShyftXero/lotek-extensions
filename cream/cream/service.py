"""CREAM domain operations — totals, rate-card sync, and the issue/freeze lifecycle.

Kept free of Flask/host imports so it is unit-testable and reusable. The one invariant enforced here:
**an issued document is immutable.** A draft tracks its engagement live; issuing freezes a numbered
snapshot (client copy == your copy); anything that would mutate an issued/void document raises.

Money arithmetic is ``Decimal`` throughout (:mod:`cream.money`). ``totals`` is the single owner of the
subtotal → discount → tax → total order; the renderer and the API both read it rather than re-deriving.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cream.enums import DEFAULT_UNIT, DocKind, DocStatus
from cream.models import Brand, Document, LineItem, NumberCounter, RateItem
from cream.money import ZERO, money, pct
from cream.viewmodel import TotalsView, build_view, snapshot_dates


class DocumentFrozen(Exception):
    """Raised on any attempt to mutate an issued or void document — it is a financial record."""


class NotConvertible(Exception):
    """Raised when a document cannot become an invoice (wrong kind, or still a draft / already void)."""


_NUMBER_PREFIX = {DocKind.quote: "Q", DocKind.invoice: "INV"}
_HUNDRED = Decimal("100")

#: A quote may be converted into an invoice once it has left draft and has not been voided. A draft
#: quote is a thing you are still writing; a void one was cancelled. Both are refused loudly.
_CONVERTIBLE_STATUSES = frozenset({DocStatus.issued, DocStatus.sent, DocStatus.accepted})


# --- totals ------------------------------------------------------------------------------------------


def compute_totals(
    amounts,
    *,
    discount_pct: Decimal = ZERO,
    discount_amount: Decimal | None = None,
    discount_label: str | None = None,
    tax_pct: Decimal = ZERO,
    tax_label: str | None = None,
) -> TotalsView:
    """subtotal → discount → taxable → tax → total, all ``Decimal``.

    A fixed ``discount_amount`` wins over ``discount_pct`` when both are set (the specific beats the
    general), and the discount is clamped to the subtotal — a discount larger than the bill would
    otherwise render a negative total, which is not a document anyone meant to produce.
    """
    subtotal = money(sum((money(a) for a in amounts), ZERO))
    if discount_amount is not None:
        discount = money(discount_amount)
    else:
        discount = money(subtotal * pct(discount_pct) / _HUNDRED)
    discount = max(ZERO, min(discount, subtotal))
    taxable = money(subtotal - discount)
    tax = money(taxable * pct(tax_pct) / _HUNDRED)
    return TotalsView(
        subtotal=subtotal,
        discount=discount,
        discount_label=discount_label or "Discount",
        taxable=taxable,
        tax=tax,
        tax_label=tax_label or "",
        total=money(taxable + tax),
    )


def totals(doc: Document) -> TotalsView:
    """The document's totals, from its live line items and its own discount/tax settings."""
    return compute_totals(
        (li.amount for li in doc.line_items),
        discount_pct=money(doc.discount_pct),
        discount_amount=None if doc.discount_amount is None else money(doc.discount_amount),
        discount_label=doc.discount_label,
        tax_pct=money(doc.tax_pct),
        tax_label=doc.tax_label,
    )


# --- branding ----------------------------------------------------------------------------------------


def get_brand(db: Session) -> Brand:
    """The singleton issuer identity, created on first read so no caller has to handle its absence."""
    brand = db.scalar(select(Brand).where(Brand.slot == "default"))
    if brand is None:
        brand = Brand(slot="default")
        db.add(brand)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:  # another request created it between the SELECT and the INSERT
            brand = db.scalar(select(Brand).where(Brand.slot == "default"))
            if brand is None:  # pragma: no cover - only reachable if the row was deleted concurrently
                raise
    return brand


# --- editing -----------------------------------------------------------------------------------------


def assert_editable(doc: Document) -> None:
    if doc.status is not DocStatus.draft:
        raise DocumentFrozen(f"document {doc.id} is {doc.status.value}; issued/void documents are immutable")


#: Fields a client may set on a DRAFT, and how long each may be. A whitelist rather than ``setattr`` over
#: the request body: the API and the editor form both funnel through here, so neither can reach
#: ``status``, ``number``, ``engagement_id``, or ``snapshot_json`` — the fields that would let a caller
#: forge a document's identity or its tenancy.
_TEXT_FIELDS = {
    "title": 255,
    "currency": 8,
    "reference": 64,
    "bill_to_name": 255,
    "bill_to_attn": 255,
    "bill_to_email": 255,
    "discount_label": 64,
    "tax_label": 64,
    "signatory_name": 255,
    "signatory_title": 255,
}
_LONGTEXT_FIELDS = ("notes", "bill_to_address", "roe_terms")
_DATE_FIELDS = ("valid_until", "due_date", "window_start", "window_end")


def _as_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def update_document(db: Session, doc: Document, data: dict) -> Document:
    """Apply the editable subset of ``data`` to a DRAFT. Unknown keys are ignored rather than rejected —
    the editor posts the whole form, and a field this version does not know about is not an error."""
    assert_editable(doc)
    for name, limit in _TEXT_FIELDS.items():
        if name in data:
            raw = data.get(name)
            value = None if raw is None else str(raw).strip()[:limit]
            setattr(doc, name, value or None)
    if not doc.title:
        doc.title = "Untitled"
    if not doc.currency:
        doc.currency = "USD"
    for name in _LONGTEXT_FIELDS:
        if name in data:
            raw = data.get(name)
            setattr(doc, name, (str(raw) if raw not in (None, "") else None))
    for name in _DATE_FIELDS:
        if name in data:
            setattr(doc, name, _as_date(data.get(name)))
    if "discount_pct" in data:
        doc.discount_pct = pct(data.get("discount_pct"))
    if "discount_amount" in data:
        raw = data.get("discount_amount")
        doc.discount_amount = None if raw in (None, "") else money(raw)
    if "tax_pct" in data:
        doc.tax_pct = pct(data.get("tax_pct"))
    if "authorization_required" in data:
        doc.authorization_required = bool(data.get("authorization_required"))
    db.flush()
    return doc


def add_line_item(db: Session, doc: Document, *, description: str, qty=1, unit_price=0,
                  unit: str = DEFAULT_UNIT, detail: str | None = None,
                  source: str = "manual") -> LineItem:
    assert_editable(doc)
    order = (max((li.order_index for li in doc.line_items), default=-1)) + 1
    li = LineItem(document_id=doc.id, description=description, detail=detail,
                  qty=money(qty, default=Decimal("1.00")), unit=(unit or DEFAULT_UNIT)[:16],
                  unit_price=money(unit_price), source=source, order_index=order)
    # Appended to the relationship, not merely `db.add`-ed: with `expire_on_commit=False` the loaded
    # collection is not re-read after a flush, so a caller that adds a line and then asks for `totals`
    # in the same transaction would otherwise be told the line does not exist.
    doc.line_items.append(li)
    db.flush()
    return li


def replace_line_items(db: Session, doc: Document, items: list[dict]) -> None:
    """Replace a draft's lines wholesale, in the given order.

    The editor is a form over the *whole* document; a diffing protocol would be more surgical and would
    also be a second source of truth about which line is which. Delete-and-rebuild is correct here
    because line items carry no identity anything else references — and the moment a document starts to
    matter (issue) it stops being editable at all.
    """
    assert_editable(doc)
    doc.line_items.clear()
    db.flush()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or "").strip()[:512]
        detail = raw.get("detail")
        if not description and not str(detail or "").strip():
            continue  # a blank row is the editor's empty state, not a line item
        doc.line_items.append(
            LineItem(
                document_id=doc.id,
                description=description or "Item",
                detail=(str(detail) if detail not in (None, "") else None),
                qty=money(raw.get("qty"), default=Decimal("1.00")),
                unit=(str(raw.get("unit") or DEFAULT_UNIT))[:16],
                unit_price=money(raw.get("unit_price")),
                source=str(raw.get("source") or "manual")[:128],
                order_index=index,
            )
        )
    db.flush()


def set_scope(db: Session, doc: Document, targets: list[str]) -> None:
    """Freeze the engagement's targets onto a DRAFT as the document's scope-of-record."""
    assert_editable(doc)
    clean = [str(t).strip()[:255] for t in targets if str(t).strip()]
    doc.scope_json = json.dumps(clean[:500], ensure_ascii=False) if clean else None
    db.flush()


# --- rate-card sync ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Suggestion:
    unit_key: str
    label: str
    unit_price: Decimal
    unit: str = DEFAULT_UNIT


def suggest_line_items(
    db: Session, doc: Document, present_unit_keys: list[str], rate_card_id: uuid.UUID | None = None
) -> list[Suggestion]:
    """Billable units present in the engagement that are NOT yet a line item on ``doc``, resolved against
    an active rate card. The caller (host route) computes ``present_unit_keys`` from the engagement's jobs;
    standalone passes ``[]``. Returns SUGGESTIONS — nothing is priced without a human accepting them."""
    assert_editable(doc)
    already = {li.source for li in doc.line_items}
    stmt = select(RateItem).where(RateItem.active.is_(True))
    if rate_card_id is not None:
        stmt = stmt.where(RateItem.rate_card_id == rate_card_id)
    by_key = {r.unit_key: r for r in db.scalars(stmt).all()}
    out: list[Suggestion] = []
    for key in present_unit_keys:
        if key in already:
            continue  # already billed on this doc
        item = by_key.get(key)
        if item is None:
            continue  # no rate-card entry for this unit — a human adds it manually
        out.append(Suggestion(unit_key=key, label=item.label, unit_price=money(item.unit_price),
                              unit=item.default_unit or DEFAULT_UNIT))
    return out


# --- numbering ---------------------------------------------------------------------------------------


def _seed_counter_value(db: Session, kind: DocKind, year: int) -> int:
    """The highest sequence already issued for ``(kind, year)``.

    Only consulted when a counter row is first created, so a database numbered by the previous MAX-scan
    scheme continues its sequence instead of restarting at 0001 and colliding on the sparse-unique.
    """
    like = f"{_NUMBER_PREFIX[kind]}-{year}-%"
    seq = 0
    for existing in db.scalars(select(Document.number).where(Document.number.like(like))).all():
        try:
            seq = max(seq, int(str(existing).rsplit("-", 1)[-1]))
        except ValueError:
            continue
    return seq


def _next_number(db: Session, kind: DocKind, year: int) -> str:
    """``INV-2026-0001`` / ``Q-2026-0001``, from a locked per-``(kind, year)`` counter row.

    The previous implementation took a MAX over already-issued numbers, so two concurrent issues read
    the same maximum and raced onto the same number. Here the row is locked for the rest of the
    transaction (``SELECT … FOR UPDATE``; a no-op on SQLite, which serializes writers anyway), so the
    second issuer waits and then reads the incremented value.
    """
    stmt = select(NumberCounter).where(NumberCounter.kind == kind.value, NumberCounter.year == year)
    counter = db.scalars(stmt.with_for_update()).one_or_none()
    if counter is None:
        try:
            with db.begin_nested():
                counter = NumberCounter(kind=kind.value, year=year,
                                        value=_seed_counter_value(db, kind, year))
                db.add(counter)
                db.flush()
        except IntegrityError:  # a concurrent issuer created it first — take theirs and lock it
            counter = db.scalars(stmt.with_for_update()).one()
    counter.value += 1
    db.flush()
    return f"{_NUMBER_PREFIX[kind]}-{year}-{counter.value:04d}"


# --- lifecycle ---------------------------------------------------------------------------------------


def issue(db: Session, doc: Document, *, brand: Brand | None = None,
          now: datetime | None = None) -> Document:
    """Freeze a draft into an immutable, numbered document. Re-issuing raises.

    The snapshot captures the **rendered view** — lines, totals, bill-to *and the issuer block* — because
    branding lives outside the document and would otherwise keep changing underneath an invoice that has
    already been sent.
    """
    if doc.status is not DocStatus.draft:
        raise DocumentFrozen(f"document {doc.id} is already {doc.status.value}")
    stamp = now or datetime.now(UTC)
    doc.number = _next_number(db, doc.kind, stamp.year)
    doc.issued_at = stamp
    doc.status = DocStatus.issued
    view = build_view(doc, brand if brand is not None else get_brand(db))
    snapshot_dates(view, doc)
    view.status = DocStatus.issued.value
    doc.snapshot_json = view.to_snapshot()
    db.flush()
    return doc


def mark_sent(db: Session, doc: Document, *, now: datetime | None = None) -> Document:
    """Record delivery to the client. Only an issued (or accepted) document can be sent — sending a
    draft would mean sending an unnumbered document that can still change."""
    if doc.status not in (DocStatus.issued, DocStatus.accepted):
        raise DocumentFrozen(f"document {doc.id} is {doc.status.value}; only an issued document is sent")
    doc.sent_at = now or datetime.now(UTC)
    doc.status = DocStatus.sent
    db.flush()
    return doc


def accept(db: Session, doc: Document, *, now: datetime | None = None) -> Document:
    """Record the client's approval of a QUOTE — the event that unlocks conversion to an invoice."""
    if doc.kind is not DocKind.quote:
        raise NotConvertible(f"document {doc.id} is an invoice; only a quote is accepted")
    if doc.status not in (DocStatus.issued, DocStatus.sent):
        raise DocumentFrozen(f"quote {doc.id} is {doc.status.value}; only an issued/sent quote is accepted")
    doc.accepted_at = now or datetime.now(UTC)
    doc.status = DocStatus.accepted
    db.flush()
    return doc


def void(db: Session, doc: Document) -> Document:
    """Cancel a document. Issued documents are never deleted — only voided — so the record survives."""
    doc.status = DocStatus.void
    db.flush()
    return doc


def convert_to_invoice(db: Session, quote: Document, *, owner_id: uuid.UUID | None = None,
                       created_by: str | None = None) -> Document:
    """Create a NEW invoice draft from an issued/accepted quote, copying every field a human would
    otherwise retype. The quote itself is left untouched.

    Copy, never mutate: a quote that turned into an invoice in place would destroy the record of what
    was quoted — which is the document the client actually agreed to.
    """
    if quote.kind is not DocKind.quote:
        raise NotConvertible(f"document {quote.id} is already an invoice")
    if quote.status not in _CONVERTIBLE_STATUSES:
        raise NotConvertible(
            f"quote {quote.id} is {quote.status.value}; issue it (and ideally have it accepted) first"
        )
    invoice = Document(
        kind=DocKind.invoice,
        status=DocStatus.draft,
        engagement_id=quote.engagement_id,
        client_id=quote.client_id,
        title=quote.title,
        currency=quote.currency,
        notes=quote.notes,
        reference=quote.reference,
        bill_to_name=quote.bill_to_name,
        bill_to_attn=quote.bill_to_attn,
        bill_to_address=quote.bill_to_address,
        bill_to_email=quote.bill_to_email,
        window_start=quote.window_start,
        window_end=quote.window_end,
        discount_pct=quote.discount_pct,
        discount_amount=quote.discount_amount,
        discount_label=quote.discount_label,
        tax_label=quote.tax_label,
        tax_pct=quote.tax_pct,
        scope_json=quote.scope_json,
        converted_from_id=quote.id,
        owner_id=owner_id,
        created_by=created_by,
    )
    db.add(invoice)
    db.flush()
    for index, li in enumerate(quote.line_items):
        db.add(LineItem(document_id=invoice.id, description=li.description, detail=li.detail,
                        qty=li.qty, unit=li.unit, unit_price=li.unit_price, source=li.source,
                        order_index=index))
    db.flush()
    db.refresh(invoice)
    return invoice


# --- burn --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BurnRow:
    """One line's quoted-vs-executed comparison. Advisory only — nothing here changes a price."""

    description: str
    source: str
    unit: str
    quoted_qty: Decimal
    executed_qty: Decimal | None

    @property
    def delta(self) -> Decimal | None:
        return None if self.executed_qty is None else money(self.executed_qty - self.quoted_qty)


def burn_rows(doc: Document, executed: dict | None) -> list[BurnRow]:
    """Compare each line's quoted quantity against what the engagement actually executed.

    ``executed`` maps a line's ``source`` (its rate-card ``unit_key``) to the quantity the host measured.
    A line with no measurement gets ``None`` rather than zero: "not measured" and "took no time" are
    different statements, and rendering the second when you mean the first invites somebody to bill zero.
    """
    measured = executed or {}
    return [
        BurnRow(
            description=li.description,
            source=li.source,
            unit=li.unit or DEFAULT_UNIT,
            quoted_qty=money(li.qty),
            executed_qty=None if measured.get(li.source) is None else money(measured.get(li.source)),
        )
        for li in doc.line_items
    ]
