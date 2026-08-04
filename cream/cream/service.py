"""CREAM domain operations — totals, rate-card sync, and the issue/freeze lifecycle.

Kept free of Flask/host imports so it is unit-testable and reusable. The one invariant enforced here:
**an issued document is immutable.** A draft tracks its engagement live; issuing freezes a numbered
snapshot (client copy == your copy); anything that would mutate an issued/void document raises.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cream.enums import DocKind, DocStatus
from cream.models import Document, LineItem, RateItem


class DocumentFrozen(Exception):
    """Raised on any attempt to mutate an issued or void document — it is a financial record."""


_NUMBER_PREFIX = {DocKind.quote: "Q", DocKind.invoice: "INV"}


def totals(doc: Document) -> dict[str, float]:
    subtotal = sum(li.amount for li in doc.line_items)
    return {"subtotal": round(subtotal, 2), "total": round(subtotal, 2)}


def assert_editable(doc: Document) -> None:
    if doc.status is not DocStatus.draft:
        raise DocumentFrozen(f"document {doc.id} is {doc.status.value}; issued/void documents are immutable")


@dataclass(frozen=True)
class Suggestion:
    unit_key: str
    label: str
    unit_price: float


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
        out.append(Suggestion(unit_key=key, label=item.label, unit_price=float(item.unit_price)))
    return out


def add_line_item(db: Session, doc: Document, *, description: str, qty: float = 1, unit_price: float = 0,
                  source: str = "manual") -> LineItem:
    assert_editable(doc)
    order = (max((li.order_index for li in doc.line_items), default=-1)) + 1
    li = LineItem(document_id=doc.id, description=description, qty=qty, unit_price=unit_price,
                  source=source, order_index=order)
    db.add(li)
    db.flush()
    return li


def _next_number(db: Session, kind: DocKind, year: int) -> str:
    """``INV-2026-0001`` / ``Q-2026-0001``. Sequential within (kind, year).

    MVP: derives the sequence from a MAX over already-issued numbers inside the caller's transaction.
    Concurrent issues of the same (kind, year) can still collide on the sparse-unique ``number`` — the
    DB unique constraint makes that a loud IntegrityError to retry, never a silent duplicate. A hardened
    counter (a per-(kind,year) sequence row with SELECT ... FOR UPDATE) is an owed follow-up."""
    prefix = _NUMBER_PREFIX[kind]
    like = f"{prefix}-{year}-%"
    rows = db.scalars(select(Document.number).where(Document.number.like(like))).all()
    seq = 0
    for n in rows:
        try:
            seq = max(seq, int(str(n).rsplit("-", 1)[-1]))
        except ValueError:
            continue
    return f"{prefix}-{year}-{seq + 1:04d}"


def issue(db: Session, doc: Document, *, now: datetime | None = None) -> Document:
    """Freeze a draft into an immutable, numbered document. Idempotent-safe: re-issuing raises."""
    if doc.status is not DocStatus.draft:
        raise DocumentFrozen(f"document {doc.id} is already {doc.status.value}")
    stamp = now or datetime.now(UTC)
    doc.number = _next_number(db, doc.kind, stamp.year)
    doc.issued_at = stamp
    doc.status = DocStatus.issued
    doc.snapshot_json = json.dumps(
        {
            "number": doc.number,
            "issued_at": stamp.isoformat(),
            "title": doc.title,
            "currency": doc.currency,
            "line_items": [
                {"description": li.description, "qty": float(li.qty),
                 "unit_price": float(li.unit_price), "amount": li.amount}
                for li in doc.line_items
            ],
            "totals": totals(doc),
        },
        ensure_ascii=False,
    )
    db.flush()
    return doc


def void(db: Session, doc: Document) -> Document:
    """Cancel a document. Issued documents are never deleted — only voided — so the record survives."""
    doc.status = DocStatus.void
    db.flush()
    return doc
