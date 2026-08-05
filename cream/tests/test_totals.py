"""Totals: order of operations, exactness, and the clamps."""

from __future__ import annotations

from decimal import Decimal

from cream.service import compute_totals


def test_subtotal_is_the_sum_of_amounts():
    tot = compute_totals([Decimal("100.00"), Decimal("249.99")])
    assert tot.subtotal == Decimal("349.99")
    assert tot.total == Decimal("349.99")


def test_tax_applies_after_the_discount_not_before():
    tot = compute_totals([Decimal("1000.00")], discount_pct=Decimal("10"), tax_pct=Decimal("20"))
    assert tot.discount == Decimal("100.00")
    assert tot.taxable == Decimal("900.00")
    assert tot.tax == Decimal("180.00")     # 20% of 900, NOT of 1000
    assert tot.total == Decimal("1080.00")


def test_fixed_discount_overrides_the_percentage():
    tot = compute_totals([Decimal("1000.00")], discount_pct=Decimal("50"),
                         discount_amount=Decimal("75.00"))
    assert tot.discount == Decimal("75.00")
    assert tot.total == Decimal("925.00")


def test_discount_cannot_drive_the_total_negative():
    tot = compute_totals([Decimal("100.00")], discount_amount=Decimal("500.00"))
    assert tot.discount == Decimal("100.00")
    assert tot.total == Decimal("0.00")


def test_negative_discount_is_not_a_surcharge():
    tot = compute_totals([Decimal("100.00")], discount_amount=Decimal("-50.00"))
    assert tot.discount == Decimal("0.00")
    assert tot.total == Decimal("100.00")


def test_percentages_beyond_a_hundred_are_clamped():
    tot = compute_totals([Decimal("100.00")], tax_pct=Decimal("900"))
    assert tot.tax == Decimal("100.00")


def test_labels_travel_with_the_numbers():
    tot = compute_totals([Decimal("10.00")], tax_pct=Decimal("20"), tax_label="VAT 20%",
                         discount_pct=Decimal("5"), discount_label="Repeat client")
    assert tot.tax_label == "VAT 20%"
    assert tot.discount_label == "Repeat client"


def test_default_discount_label_when_unset():
    assert compute_totals([Decimal("10.00")]).discount_label == "Discount"


def test_empty_document_totals_zero_not_none():
    tot = compute_totals([])
    assert tot.subtotal == Decimal("0.00")
    assert tot.total == Decimal("0.00")


def test_rounding_is_half_up_at_the_cent():
    tot = compute_totals([Decimal("100.00")], tax_pct=Decimal("8.25"))
    assert tot.tax == Decimal("8.25")
    tot2 = compute_totals([Decimal("33.33")], tax_pct=Decimal("7.5"))
    assert tot2.tax == Decimal("2.50")  # 2.49975 -> 2.50
