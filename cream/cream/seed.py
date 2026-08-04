"""Idempotent first-boot seed — a starter rate card so the sync flow has something to suggest.

Named by the manifest ``seed = "cream.seed:seed_defaults"``; lotek runs it once after ``register()``,
inside a session, and it is safe to re-run on every boot.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cream.models import RateCard, RateItem

_DEFAULT_CARD = "Default"

# (unit_key, label, unit_price) — a billable unit maps to a suggested line item. Edit freely in the UI.
_DEFAULT_ITEMS = [
    ("run_type:external_pentest", "External network penetration test", 5000),
    ("run_type:internal_pentest", "Internal network penetration test", 6000),
    ("run_type:web_app", "Web application assessment", 4500),
    ("run_type:ad_assessment", "Active Directory assessment", 5500),
    ("phase:retest", "Remediation retest", 1500),
]


def seed_defaults(session: Session) -> None:
    card = session.scalar(select(RateCard).where(RateCard.name == _DEFAULT_CARD))
    if card is None:
        card = RateCard(name=_DEFAULT_CARD, currency="USD", active=True)
        session.add(card)
        session.flush()
    existing = {r.unit_key for r in card.items}
    for unit_key, label, price in _DEFAULT_ITEMS:
        if unit_key not in existing:
            session.add(RateItem(rate_card_id=card.id, unit_key=unit_key, label=label, unit_price=price))
    session.commit()
