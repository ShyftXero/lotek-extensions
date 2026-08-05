"""Numbering, and the quote → accepted → invoice path."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cream.enums import DocKind, DocStatus
from cream.models import Document, NumberCounter
from cream.service import (
    DocumentFrozen,
    NotConvertible,
    accept,
    add_line_item,
    convert_to_invoice,
    issue,
    mark_sent,
)


def _make(db, engagement_id, kind=DocKind.invoice, **kw):
    doc = Document(engagement_id=engagement_id, kind=kind, title="Assessment", **kw)
    db.add(doc)
    db.flush()
    add_line_item(db, doc, description="Testing", qty=1, unit="project", unit_price=1000)
    return doc


# --- numbering ---------------------------------------------------------------------------------


def test_numbers_are_sequential_within_kind_and_year(session_factory, engagement_id):
    with session_factory() as db:
        first = _make(db, engagement_id)
        second = _make(db, engagement_id)
        quote = _make(db, engagement_id, kind=DocKind.quote)
        stamp = datetime(2026, 5, 1, tzinfo=UTC)
        issue(db, first, now=stamp)
        issue(db, second, now=stamp)
        issue(db, quote, now=stamp)
        db.commit()
        assert first.number == "INV-2026-0001"
        assert second.number == "INV-2026-0002"
        assert quote.number == "Q-2026-0001"  # its own sequence


def test_the_sequence_restarts_each_year(session_factory, engagement_id):
    with session_factory() as db:
        old = _make(db, engagement_id)
        new = _make(db, engagement_id)
        issue(db, old, now=datetime(2026, 12, 31, tzinfo=UTC))
        issue(db, new, now=datetime(2027, 1, 2, tzinfo=UTC))
        db.commit()
        assert old.number == "INV-2026-0001"
        assert new.number == "INV-2027-0001"


def test_a_counter_created_late_continues_from_existing_numbers(session_factory, engagement_id):
    """A database numbered by the old MAX-scan gets a counter seeded from it, not a restart at 0001."""
    with session_factory() as db:
        legacy = _make(db, engagement_id)
        legacy.number = "INV-2026-0007"
        legacy.status = DocStatus.issued
        db.commit()

    with session_factory() as db:
        assert db.query(NumberCounter).count() == 0  # nothing has issued through the counter yet
        fresh = _make(db, engagement_id)
        issue(db, fresh, now=datetime(2026, 6, 1, tzinfo=UTC))
        db.commit()
        assert fresh.number == "INV-2026-0008"


# --- send / accept -----------------------------------------------------------------------------


def test_a_draft_cannot_be_sent(session_factory, engagement_id):
    with session_factory() as db:
        doc = _make(db, engagement_id)
        with pytest.raises(DocumentFrozen):
            mark_sent(db, doc)


def test_issue_then_send_records_the_timestamp(session_factory, engagement_id):
    with session_factory() as db:
        doc = _make(db, engagement_id)
        issue(db, doc)
        mark_sent(db, doc)
        db.commit()
        assert doc.status is DocStatus.sent
        assert doc.sent_at is not None


def test_only_a_quote_can_be_accepted(session_factory, engagement_id):
    with session_factory() as db:
        invoice = _make(db, engagement_id)
        issue(db, invoice)
        with pytest.raises(NotConvertible):
            accept(db, invoice)


def test_a_draft_quote_cannot_be_accepted(session_factory, engagement_id):
    with session_factory() as db:
        quote = _make(db, engagement_id, kind=DocKind.quote)
        with pytest.raises(DocumentFrozen):
            accept(db, quote)


# --- convert -----------------------------------------------------------------------------------


def test_convert_copies_the_quote_into_a_new_invoice_draft(session_factory, engagement_id):
    with session_factory() as db:
        quote = _make(db, engagement_id, kind=DocKind.quote, tax_label="VAT 20%",
                      tax_pct=Decimal("20"), reference="PO-99")
        quote.bill_to_name = "Acme Corp"
        quote.scope_json = '["10.0.0.0/24"]'
        issue(db, quote)
        accept(db, quote)
        db.commit()
        quote_id, quote_number = quote.id, quote.number

        invoice = convert_to_invoice(db, quote)
        db.commit()

        assert invoice.kind is DocKind.invoice
        assert invoice.status is DocStatus.draft
        assert invoice.number is None                  # a draft has no number
        assert invoice.converted_from_id == quote_id
        assert invoice.bill_to_name == "Acme Corp"
        assert invoice.tax_label == "VAT 20%"
        assert invoice.reference == "PO-99"
        assert invoice.scope_json == '["10.0.0.0/24"]'
        assert [li.description for li in invoice.line_items] == ["Testing"]

        # the quote itself is untouched — it is still the record of what was agreed
        preserved = db.get(Document, quote_id)
        assert preserved.status is DocStatus.accepted
        assert preserved.number == quote_number


def test_a_draft_quote_cannot_be_converted(session_factory, engagement_id):
    with session_factory() as db:
        quote = _make(db, engagement_id, kind=DocKind.quote)
        with pytest.raises(NotConvertible):
            convert_to_invoice(db, quote)


def test_an_invoice_cannot_be_converted_again(session_factory, engagement_id):
    with session_factory() as db:
        invoice = _make(db, engagement_id)
        issue(db, invoice)
        with pytest.raises(NotConvertible):
            convert_to_invoice(db, invoice)


def test_a_voided_quote_cannot_be_converted(session_factory, engagement_id):
    from cream.service import void

    with session_factory() as db:
        quote = _make(db, engagement_id, kind=DocKind.quote)
        issue(db, quote)
        void(db, quote)
        with pytest.raises(NotConvertible):
            convert_to_invoice(db, quote)
