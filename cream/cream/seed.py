"""Idempotent first-boot seed — the issuer singleton and a starter rate card so the sync flow has
something to suggest.

Named by the manifest ``seed = "cream.seed:seed_defaults"``; lotek runs it once after ``register()``,
inside a session, and it is safe to re-run on every boot.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cream.models import RateCard, RateItem
from cream.service import get_brand

_DEFAULT_CARD = "Default"

# (unit_key, label, unit_price, default_unit) — a billable unit maps to a suggested line item. The unit
# is what makes flat-rate and hourly the same mechanism: an assessment is 1 × project, a retest is
# priced by the hour. Edit freely in the UI.
_DEFAULT_ITEMS = [
    ("run_type:external_pentest", "External network penetration test", Decimal("5000"), "project"),
    ("run_type:internal_pentest", "Internal network penetration test", Decimal("6000"), "project"),
    ("run_type:web_app", "Web application assessment", Decimal("4500"), "project"),
    ("run_type:ad_assessment", "Active Directory assessment", Decimal("5500"), "project"),
    ("phase:retest", "Remediation retest", Decimal("250"), "hr"),
    ("phase:reporting", "Reporting and readout", Decimal("225"), "hr"),
    ("host-band:1-256", "External host coverage", Decimal("35"), "host"),
]


def seed_defaults(session: Session) -> None:
    get_brand(session)  # create the issuer singleton so the branding page has a row to edit
    card = session.scalar(select(RateCard).where(RateCard.name == _DEFAULT_CARD))
    if card is None:
        card = RateCard(name=_DEFAULT_CARD, currency="USD", active=True)
        session.add(card)
        session.flush()
    existing = {r.unit_key for r in card.items}
    for unit_key, label, price, unit in _DEFAULT_ITEMS:
        if unit_key not in existing:
            session.add(RateItem(rate_card_id=card.id, unit_key=unit_key, label=label,
                                 unit_price=price, default_unit=unit))
    session.commit()
