"""Money is ``Decimal``, never ``float``.

CREAM stores amounts as ``Numeric(12, 2)`` and must arithmetic them exactly: an invoice whose lines sum
to a cent off the total is not a rounding curiosity, it is a document a client disputes. Before this
module the model annotated those columns ``Mapped[float]`` and ``LineItem.amount`` multiplied two floats
— ``0.1 * 3`` money.

Three rules, and the whole module exists to make them mechanical:

* **Coerce through ``str``.** ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827``; ``Decimal("0.1")``
  is a dime. Anything arriving from JSON is a float and must go through :func:`money`.
* **Quantize at every boundary**, half-up — the rounding a human does on paper, not banker's rounding.
* **``float()`` only at the JSON edge** (:func:`as_json`). JSON has no decimal type; nothing reads that
  float back into a stored amount without re-coercing.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, DecimalException

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")
#: Amount columns are ``Numeric(12, 2)`` — ten integer digits. Reject beyond it rather than let the DB
#: raise on INSERT (or, on SQLite, silently store something else).
MAX_AMOUNT = Decimal("9999999999.99")


def money(value, *, default: Decimal = ZERO) -> Decimal:
    """Coerce anything to a 2-dp ``Decimal``, half-up. Junk (``None``, ``""``, ``"abc"``, NaN) -> ``default``.

    Tolerant on purpose: the callers are an HTML form and a JSON body, where a blank field is the normal
    way to say "unset" and must not 500. Malformed *values* still degrade to the default rather than
    propagating a NaN through the totals.
    """
    if value is None or value == "":
        return default
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value).strip().replace(",", ""))
    except (DecimalException, ValueError, TypeError, AttributeError):
        return default
    if not dec.is_finite():  # NaN / Infinity — JSON can carry them, Numeric cannot
        return default
    if dec > MAX_AMOUNT:
        dec = MAX_AMOUNT
    elif dec < -MAX_AMOUNT:
        dec = -MAX_AMOUNT
    return dec.quantize(CENTS, rounding=ROUND_HALF_UP)


def pct(value, *, default: Decimal = ZERO) -> Decimal:
    """A percentage, 2 dp, clamped to 0–100. Same tolerance as :func:`money`.

    Clamped rather than validated because a percentage arrives from a number input a human can type
    ``1000`` into, and a 1000% tax line is a wrong document, not an error page.
    """
    got = money(value, default=default)
    if got < ZERO:
        return ZERO
    return Decimal("100.00") if got > Decimal("100") else got


def as_json(value: Decimal | None) -> float | None:
    """The JSON boundary — ``Decimal`` out, ``float`` in the payload. Never feed the result back into a
    stored amount without :func:`money`."""
    return None if value is None else float(value)


def fmt(value: Decimal | None) -> str:
    """Thousands-separated 2-dp string for rendering: ``Decimal('1234.5') -> '1,234.50'``."""
    return f"{money(value):,.2f}"
