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
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, String, Text, Uuid, select
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


# ── CVE -> severity derivation (replaces the product/version ABC) ──────────────────────────
#
# The original ``EnrichmentDriver`` ABC mapped ``(product, version) -> severity`` — a shape that never
# fit the CVE model (the operator: "don't shoehorn what doesn't fit"). A CVE already carries a CVSS
# base score, so the suggested severity is a PURE derivation from that score, not a second lookup. This
# is the reusable primitive the proposal path (and, later, the CVE-detail source) derive ``suggested``
# from; the live CVSS value arrives with the swappable CVE-detail source (#674 follow-on).

def severity_from_cvss(score: float | int | None) -> Severity | None:
    """The CVSS v3 qualitative band for a base ``score``, or ``None`` when there is no usable score
    (``None``, a bool, out of the 0–10 range). The bands are the FIRST.org standard
    (0.0 none, 0.1–3.9 low, 4.0–6.9 medium, 7.0–8.9 high, 9.0–10.0 critical); 0.0 maps to ``info``
    (scribble has no ``none`` band). A bad score yields ``None`` -> NO proposal (never a wrong one)."""
    if score is None or isinstance(score, bool):
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 10:
        return None
    if value == 0:
        return Severity.info
    if value < 4.0:
        return Severity.low
    if value < 7.0:
        return Severity.medium
    if value < 9.0:
        return Severity.high
    return Severity.critical


def is_promotion(current: Severity, suggested: Severity) -> bool:
    """Promote-only: a proposal is valid only if it RAISES severity."""
    return _ORDER.get(suggested, 0) > _ORDER.get(current, 0)


def propose(db, *, finding_id: uuid.UUID, engagement_id: uuid.UUID, current: Severity,
            suggested: Severity, reason: str, source: str) -> EnrichmentProposal | None:
    """Record a promotion proposal, or None if it is not a promotion (promote-only guard).

    IDEMPOTENT: at most ONE pending proposal per ``(finding_id, source)``. Re-running promotion over a
    finding a source already flagged returns the EXISTING pending row instead of stacking a duplicate
    the operator would have to dismiss N times — first-wins, so a decided (accepted/rejected) proposal
    does not block a fresh one, only an undecided one does. The uniqueness is scoped to ``source`` so
    two different sources can each stand a proposal for the same finding.

    The guard is a SELECT, not a DB unique constraint: proposals are generated at promotion (serialized
    per engagement in one txn) so two racers for the same ``(finding_id, source)`` is not a path the
    product produces today. Add a partial-unique index if a concurrent generator ever lands (the
    CVE-detail source / proposal UI, #674 follow-on).
    """
    if not is_promotion(current, suggested):
        return None
    existing = (
        db.execute(
            select(EnrichmentProposal).where(
                EnrichmentProposal.finding_id == finding_id,
                EnrichmentProposal.source == source[:120],
                EnrichmentProposal.status == EnrichmentStatus.pending,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    row = EnrichmentProposal(
        finding_id=finding_id, engagement_id=engagement_id, current_severity=current,
        suggested_severity=suggested, reason=reason[:280], source=source[:120],
    )
    db.add(row)
    db.flush()
    return row


def decide(db, proposal: EnrichmentProposal, *, accept: bool, decided_by: uuid.UUID | None,
           note: str | None = None) -> EnrichmentProposal:
    """Human decision — accept or reject. Records who + when (defensibility).

    On ACCEPT the promotion is APPLIED to the Scribble report side: the matching ``BoardFinding`` (the
    one promoted from this core finding — ``source_finding_id == proposal.finding_id``, scoped to the
    proposal's own ``engagement_id`` so the apply can never cross a tenant) has its rendered ``severity``
    raised to the proposal's ``suggested_severity``, and the decision is AUDITED (INV-AUDIT-04). This is
    what the review flagged as owed: before this, ``decide`` set a status and nothing a report reader
    could see changed. Core scan data is never touched — only the report row moves. Full provenance
    survives: the proposal row keeps ``current_severity`` -> ``suggested_severity`` with the
    ``reason``/``source``/``decided_by``, and the audit records the ``before`` severity, so the promotion
    is reconstructable and the original computed band is never lost. A reject applies nothing. Idempotent
    apply: a re-accept just re-asserts the same severity. If the finding is no longer on the board, the
    decision is still recorded and the audit notes ``applied: false``.
    """
    from scribble.api_pat import _audit  # lazy: avoids an import cycle (api_pat imports enrichment-side)
    from scribble.models import BoardFinding

    proposal.status = EnrichmentStatus.accepted if accept else EnrichmentStatus.rejected
    proposal.decided_by = decided_by
    proposal.decided_at = datetime.now(UTC)
    proposal.note = note

    if accept:
        finding = (
            db.execute(
                select(BoardFinding).where(
                    BoardFinding.engagement_id == proposal.engagement_id,
                    BoardFinding.source_finding_id == proposal.finding_id,
                )
            )
            .scalars()
            .first()
        )
        if finding is not None:
            # severity round-trips as a Severity enum from the DB but may be a bare str on a row built
            # in-session before a reload; read it defensively (the codebase's own from_lotek_finding
            # pattern) so the audit's ``before`` is the value either way.
            before = getattr(finding.severity, "value", finding.severity) if finding.severity else None
            finding.severity = proposal.suggested_severity
            _audit(
                db, "enrichment_accepted", subject_type="finding", subject_id=finding.id,
                before={"severity": before},
                after={"severity": proposal.suggested_severity.value, "reason": proposal.reason,
                       "source": proposal.source, "proposal_id": str(proposal.id)},
            )
        else:
            _audit(
                db, "enrichment_accepted", subject_type="finding", subject_id=proposal.finding_id,
                after={"severity": proposal.suggested_severity.value, "source": proposal.source,
                       "proposal_id": str(proposal.id), "applied": False},
            )

    db.flush()
    return proposal


# ── threat_intel (KEV/EPSS) enrichment driver (#642) ────────────────────────────────────────────────
#
# DISTINCT from the severity-promotion ``EnrichmentDriver`` above: that maps product+version -> a
# severity proposal; this produces a dated KEV/EPSS SNAPSHOT (via ``scribble.metadata.build_threat_intel``)
# for a finding's CVEs from the host's ``verdicts_for_cves`` hook and writes it to ``finding.threat_intel``.
# So it is a SIBLING class, not a distortion of that ABC.
#
# SCAFFOLDING (#642): the host ``verdicts_for_cves`` hook is core-side STUBBED (no egress). This side is
# built to the fixed ``(feed, health)`` contract and gated by per-engagement egress CONSENT + AUDIT +
# honest DEGRADATION, so it is safe the moment the live feed is wired.


def _today() -> str:
    """The ``as_of`` date for a snapshot — an aware UTC date, never an argless ``date.today()`` /
    ``datetime.now()``."""
    return datetime.now(UTC).date().isoformat()


def egress_consented(engagement) -> bool:
    """The ONE per-engagement egress-consent decision (#642, INV-EGRESS-02) — every caller reads consent
    through HERE, never inline (one derived-state predicate, one home).

    Default OFF, and FORCED OFF for INTERNAL engagements: an internal engagement's CVEs never leave the
    box to a third-party feed regardless of the flag. Fails closed (False) for a missing engagement."""
    if engagement is None:
        return False
    if (getattr(engagement, "scope_type", "") or "").strip().lower() == "internal":
        return False
    return bool(getattr(engagement, "threat_intel_egress_consent", False))


class ThreatIntelDriver:
    """Produce a finding's KEV/EPSS ``threat_intel`` snapshot from the host verdict feed, or ``None``.

    ``propose(db, finding)`` gates on egress CONSENT first (no consent -> NO hook call, no lookup, no
    audit), then calls the host ``verdicts_for_cves`` hook, AUDITS the lookup (one row carrying the
    engagement, the driver, the CVE count and the health map — INV-EGRESS-02), and returns
    ``build_threat_intel(...)`` (``None`` on an empty/degraded feed — INV-DEPLOY-02 / INV-DATA-07: a
    degraded control is never a clean "nothing exploitable"). ``apply(db, finding, snap)`` writes it.

    Takes ``db`` (not the bare ``propose(finding)`` of the roadmap sketch) because the audit of every
    LOOKUP must land in the SAME session/txn as the enrichment.
    """

    name = "exploiteer"

    def propose(self, db, finding) -> dict | None:
        # Lazy imports: this module is imported by models.py for side-effect registration, so a
        # module-level import of api_pat/host/metadata risks an import cycle (host is resolved LATE
        # everywhere in this package anyway).
        from scribble import host
        from scribble.api_pat import _audit
        from scribble.metadata import build_threat_intel

        # CONSENT gate FIRST — engagement re-derived host-side from the finding's OWN row (never a
        # request value): INV-TENANCY-06/07. Off -> no hook call, no egress, no audit.
        if not egress_consented(getattr(finding, "engagement", None)):
            return None
        hook = host.verdicts_for_cves()
        if hook is None:
            return None  # exploiteer unmounted -> degrade to no snapshot (nothing was looked up)
        cve_ids = list(finding.cve_ids or [])
        feed, health = hook(cve_ids)
        # AUDIT one row per LOOKUP (consent ON + hook called): the disclosure record INV-EGRESS-02
        # requires, and — carrying ``health`` — the degraded-state signal INV-DEPLOY-02 requires.
        _audit(
            db, "threat_intel_lookup", subject_type="finding", subject_id=finding.id,
            after={"engagement_id": str(finding.engagement_id), "driver": self.name,
                   "cve_count": len(cve_ids), "health": health},
        )
        # DEGRADATION: build_threat_intel returns None for an empty / all-miss feed -> threat_intel stays
        # None (NOT a false "no threat"); the health above already recorded the degraded state.
        return build_threat_intel(cve_ids, feed, as_of=_today(), source=self.name)

    def apply(self, db, finding, snap) -> None:
        """Write the proposed snapshot onto the finding. A ``None`` snap keeps ``threat_intel`` None
        (the degraded case), so a caller can ``apply(db, f, driver.propose(db, f))`` unconditionally."""
        finding.threat_intel = snap
        db.flush()
