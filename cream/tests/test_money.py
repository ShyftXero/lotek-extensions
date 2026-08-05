"""Money is exact, or it is not money.

These pin the failure the ``Decimal`` migration exists to prevent: the old model annotated
``Numeric(12, 2)`` columns as ``float`` and multiplied them, so a quantity a human would call "three
tenths" produced a total nobody could reconcile.
"""

from __future__ import annotations

from decimal import Decimal

from cream.money import MAX_AMOUNT, as_json, fmt, money, pct


def test_float_input_is_coerced_through_str_not_binary():
    # Decimal(0.1) is 0.1000000000000000055511151231257827; Decimal("0.1") is a dime.
    assert money(0.1) == Decimal("0.10")
    assert money(2.675) == Decimal("2.68")  # half-up, not banker's rounding to 2.67


def test_the_classic_float_sum_is_exact():
    total = money(0.1) + money(0.2)
    assert total == Decimal("0.30")
    assert 0.1 + 0.2 != 0.3  # plain floats — the bug this replaces


def test_hourly_line_does_not_drift():
    assert money(money("16") * money("249.99")) == Decimal("3999.84")


def test_a_half_cent_product_rounds_up_where_a_float_multiply_rounds_down():
    """The case that actually distinguishes Decimal from float — and the reason this test exists.

    An earlier version of the suite "covered" the Decimal migration with 16 × 249.99, which quantizes to
    the same cent either way: the guard was green against a deliberate float-multiply regression, so it
    was pinning nothing. 2.5 hr at $150.07 is exactly 375.175, which rounds half-up to 375.18; the same
    multiplication in binary floating point lands a hair below the half-cent and truncates to 375.17.
    One cent per line, silently, on every invoice with a fractional quantity.
    """
    qty, rate = money("2.5"), money("150.07")
    assert money(qty * rate) == Decimal("375.18")
    assert money(Decimal(str(float(qty) * float(rate)))) == Decimal("375.17")  # the float path


def test_junk_degrades_to_the_default_rather_than_raising():
    for junk in (None, "", "  ", "abc", object(), float("nan"), float("inf")):
        assert money(junk) == Decimal("0.00")
    assert money(None, default=Decimal("1.00")) == Decimal("1.00")


def test_thousands_separators_are_tolerated():
    assert money("1,234.50") == Decimal("1234.50")


def test_amount_is_clamped_to_the_column_width():
    assert money("99999999999999") == MAX_AMOUNT
    assert money("-99999999999999") == -MAX_AMOUNT


def test_pct_clamps_to_a_percentage():
    assert pct("20") == Decimal("20.00")
    assert pct("1000") == Decimal("100.00")
    assert pct("-5") == Decimal("0.00")
    assert pct("") == Decimal("0.00")


def test_json_boundary_is_the_only_float():
    assert as_json(Decimal("12.34")) == 12.34
    assert as_json(None) is None


def test_fmt_is_thousands_separated_two_dp():
    assert fmt(Decimal("1234.5")) == "1,234.50"
    assert fmt(None) == "0.00"
