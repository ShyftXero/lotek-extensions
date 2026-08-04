"""Vuln enrichment — propose (never auto-apply) a higher severity for an under-rated finding.

The propose/apply seam (roadmap §3.2): a background lookup writes a **proposal** row (never a core
write); a human accepts or rejects; accept applies the promotion to the Scribble *report* side and
records the decision. Core scan data stays the scanner's ground truth.

**v2-native table** even though the rest of Scribble is still pre-v2 int-PK: this is a new table, so it
starts correct — UUIDv7 PK, and every core reference (``finding_id``, ``engagement_id``, ``decided_by``)
is ``sqlalchemy.Uuid`` (INV-INTEGRITY-03). ``engagement_id`` is **NOT NULL** and re-derived at proposal
time (INV-TENANCY-06) so a pending-proposal query can never disclose findings across engagements.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from scribble.db import Base
from scribble.enums import Severity

# Severity order for the promote-only guard (a proposal never suggests a LOWER severity).
_ORDER = {
    Severity.info: 0, Severity.low: 1, Severity.medium: 2, Severity.high: 3, Severity.critical: 4,
}


class EnrichmentStatus:
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"


class EnrichmentProposal(Base):
    """A proposed severity promotion for one finding, awaiting a human decision."""

    __tablename__ = "scribble_enrichment_proposals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    finding_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)   # core Finding
    engagement_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)  # NOT NULL (tenancy)
    current_severity: Mapped[Severity] = mapped_column(Enum(Severity))
    suggested_severity: Mapped[Severity] = mapped_column(Enum(Severity))
    reason: Mapped[str] = mapped_column(String(280))   # <= 1 sentence
    source: Mapped[str] = mapped_column(String(120))   # driver + record id
    status: Mapped[str] = mapped_column(String(16), default=EnrichmentStatus.pending, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # core User
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── the driver: a pluggable, read-only vuln lookup (minimal, like scribble/vector) ──────────────────

class EnrichmentDriver(ABC):
    """Maps a product+version to a severity + reference, or None. Real impl: vulnx (projectdiscovery),
    swappable. Failure yields None -> NO proposal (never a wrong one)."""

    name = "null"

    @abstractmethod
    def lookup(self, product: str, version: str) -> tuple[Severity, str] | None: ...


class NullEnrichment(EnrichmentDriver):
    """MVP backend: no external lookup, no egress. Returns None (no proposal). Keeps the whole seam
    exercisable with no network dependency; a real driver replaces this."""

    name = "null"

    def lookup(self, product: str, version: str) -> tuple[Severity, str] | None:
        return None


def is_promotion(current: Severity, suggested: Severity) -> bool:
    """Promote-only: a proposal is valid only if it RAISES severity."""
    return _ORDER.get(suggested, 0) > _ORDER.get(current, 0)


def propose(db, *, finding_id: uuid.UUID, engagement_id: uuid.UUID, current: Severity,
            suggested: Severity, reason: str, source: str) -> EnrichmentProposal | None:
    """Record a promotion proposal, or None if it is not a promotion (promote-only guard)."""
    if not is_promotion(current, suggested):
        return None
    row = EnrichmentProposal(
        finding_id=finding_id, engagement_id=engagement_id, current_severity=current,
        suggested_severity=suggested, reason=reason[:280], source=source[:120],
    )
    db.add(row)
    db.flush()
    return row


def decide(db, proposal: EnrichmentProposal, *, accept: bool, decided_by: uuid.UUID | None,
           note: str | None = None) -> EnrichmentProposal:
    """Human decision — accept or reject. Records who + when (defensibility). The report-side apply on
    accept (rendering the promoted severity) and the core AuditEvent are the owed follow-on (INV-AUDIT-04)."""
    proposal.status = EnrichmentStatus.accepted if accept else EnrichmentStatus.rejected
    proposal.decided_by = decided_by
    proposal.decided_at = datetime.now(UTC)
    proposal.note = note
    db.flush()
    return proposal
