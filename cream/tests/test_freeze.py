"""The freeze invariant — the rule the whole design rests on.

An issued document is a financial record: it cannot be edited, and it must keep rendering the way it
rendered when it was issued even after the things it was built from (branding, rate cards, the client
record) have moved on. Rendering an issued document from live rows would break that quietly, which is
why ``viewmodel.view_for`` reads the snapshot and these tests watch it do so.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cream.enums import DocStatus
from cream.models import Document
from cream.render import render_document_html
from cream.service import (
    DocumentFrozen,
    add_line_item,
    get_brand,
    issue,
    replace_line_items,
    set_scope,
    update_document,
)
from cream.viewmodel import view_for


def _doc(session_factory, engagement_id, **kw):
    with session_factory() as db:
        doc = Document(engagement_id=engagement_id, title="Assessment", **kw)
        db.add(doc)
        db.flush()
        add_line_item(db, doc, description="External test", qty=1, unit="project", unit_price=5000)
        db.commit()
        return doc.id


def test_issue_numbers_freezes_and_snapshots(session_factory, engagement_id):
    doc_id = _doc(session_factory, engagement_id)
    with session_factory() as db:
        doc = db.get(Document, doc_id)
        issue(db, doc)
        db.commit()
        assert doc.status is DocStatus.issued
        assert doc.number == f"INV-{doc.issued_at.year}-0001"
        assert doc.snapshot_json


@pytest.mark.parametrize(
    "mutate",
    [
        lambda db, doc: update_document(db, doc, {"title": "changed"}),
        lambda db, doc: add_line_item(db, doc, description="sneak", qty=1, unit_price=1),
        lambda db, doc: replace_line_items(db, doc, []),
        lambda db, doc: set_scope(db, doc, ["10.0.0.0/8"]),
    ],
)
def test_every_mutation_path_refuses_an_issued_document(session_factory, engagement_id, mutate):
    doc_id = _doc(session_factory, engagement_id)
    with session_factory() as db:
        doc = db.get(Document, doc_id)
        issue(db, doc)
        db.commit()
        with pytest.raises(DocumentFrozen):
            mutate(db, doc)


def test_reissuing_raises(session_factory, engagement_id):
    doc_id = _doc(session_factory, engagement_id)
    with session_factory() as db:
        doc = db.get(Document, doc_id)
        issue(db, doc)
        db.commit()
        with pytest.raises(DocumentFrozen):
            issue(db, doc)


def test_issued_document_renders_from_the_snapshot_after_branding_changes(session_factory,
                                                                          engagement_id):
    """The letterhead on a sent invoice must not follow the firm's current letterhead."""
    doc_id = _doc(session_factory, engagement_id)
    with session_factory() as db:
        brand = get_brand(db)
        brand.company_name = "Original Security Ltd"
        db.flush()
        doc = db.get(Document, doc_id)
        issue(db, doc, brand=brand)
        db.commit()

    with session_factory() as db:
        brand = get_brand(db)
        brand.company_name = "Renamed Holdings Inc"
        db.commit()

    with session_factory() as db:
        doc = db.get(Document, doc_id)
        html = render_document_html(view_for(doc, get_brand(db)))
        assert "Original Security Ltd" in html
        assert "Renamed Holdings Inc" not in html


def test_snapshot_keeps_money_exact_across_the_round_trip(session_factory, engagement_id):
    with session_factory() as db:
        doc = Document(engagement_id=engagement_id, title="Hourly", tax_pct=Decimal("8.25"))
        db.add(doc)
        db.flush()
        add_line_item(db, doc, description="Testing", qty="16", unit="hr", unit_price="249.99")
        issue(db, doc)
        db.commit()
        doc_id = doc.id

    with session_factory() as db:
        view = view_for(db.get(Document, doc_id))
        assert view.totals.subtotal == Decimal("3999.84")
        assert view.totals.tax == Decimal("329.99")
        assert view.totals.total == Decimal("4329.83")
        assert view.lines[0].qty_display == "16 hr"


def test_status_keeps_moving_after_the_snapshot_is_taken(session_factory, engagement_id):
    """A snapshot freezes the *content*, not the lifecycle: 'sent' must still show as sent."""
    from cream.service import mark_sent

    doc_id = _doc(session_factory, engagement_id)
    with session_factory() as db:
        doc = db.get(Document, doc_id)
        issue(db, doc)
        mark_sent(db, doc)
        db.commit()

    with session_factory() as db:
        assert view_for(db.get(Document, doc_id)).status == "sent"


def test_a_corrupt_snapshot_falls_back_to_live_rendering(session_factory, engagement_id):
    """An unreadable snapshot must not 500 a document somebody needs to look at."""
    doc_id = _doc(session_factory, engagement_id)
    with session_factory() as db:
        doc = db.get(Document, doc_id)
        issue(db, doc)
        doc.snapshot_json = "{not json"
        db.commit()

    with session_factory() as db:
        view = view_for(db.get(Document, doc_id))
        assert view.totals.total == Decimal("5000.00")
