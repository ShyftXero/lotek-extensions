"""Enumerations for Scribble.

Severity / Confidence / FindingStatus mirror Lotek's enum *values* exactly so a Lotek scan ``Finding``
can be promoted into an ``EngagementFinding`` and so the two systems reconcile cleanly at the port
checkpoint. When mounted in Lotek, ``register(..., severity_enum=...)`` can inject the host enum; these
are the standalone definitions and the canonical value set.
"""

from __future__ import annotations

import enum


class Severity(enum.StrEnum):
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class Confidence(enum.StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class FindingStatus(enum.StrEnum):
    new = "new"
    triaged = "triaged"
    accepted_risk = "accepted_risk"
    false_positive = "false_positive"
    fixed = "fixed"
    needs_retest = "needs_retest"


class RetestOutcome(enum.StrEnum):
    """The result of one retest round on a finding (lotek#621). Each value maps to the finding's
    ``FindingStatus`` in exactly one place — ``findings_service.record_retest`` — so the outcome→status
    policy never gets re-decided at a call site: a verified fix closes the finding, an unresolved one
    reopens it for another round, an accepted risk records the client's decision, and an untested
    finding leaves the status untouched (recording the attempt is not a verdict on the fix)."""

    remediated = "remediated"                      # fix verified -> FindingStatus.fixed
    partially_remediated = "partially_remediated"  # some risk remains -> FindingStatus.needs_retest
    not_remediated = "not_remediated"              # still exploitable -> FindingStatus.needs_retest
    accepted_risk = "accepted_risk"                # client accepted -> FindingStatus.accepted_risk
    not_tested = "not_tested"                      # could not retest -> status unchanged


class ChecklistKind(enum.StrEnum):
    """Engagement-checklist kind. Fixed set: the report layout and the recommended item-status
    vocabulary are keyed to it. A checklist's kind is reassignable; adding a NEW kind is a code change
    (it needs a report renderer). Checklists are non-blocking visual reminders in every kind."""

    coverage = "coverage"      # testing methodology; a failed item may link a finding
    reminder = "reminder"      # operational reminder (e.g. pre-engagement); internal by default
    compliance = "compliance"  # attestation; items carry free-text framework + control_ref


class OrderMode(enum.StrEnum):
    """How a FindingGroup orders its findings. ``manual`` persists explicit order_index."""

    auto_severity = "auto_severity"
    manual = "manual"


class ArtifactKind(enum.StrEnum):
    screenshot = "screenshot"
    text = "text"
    file = "file"
    inline_image = "inline_image"


class ArtifactPlacement(enum.StrEnum):
    attached = "attached"  # shows in the finding's evidence gallery
    inline = "inline"  # referenced from content JSON by id


class ReportFormat(enum.StrEnum):
    html = "html"
    docx = "docx"
    json = "json"  # #627: machine-readable findings export (structured data, not a rendered deliverable)
    csv = "csv"    # #627: one row per finding, for a spreadsheet / SIEM import


class VariableScope(enum.StrEnum):
    engagement = "engagement"
    finding = "finding"


class VariableType(enum.StrEnum):
    str_ = "str"
    bool_ = "bool"
    list_ = "list"
    html = "html"


# Worst-first. The single source of severity ordering for auto ranking + risk rollup.
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.critical,
    Severity.high,
    Severity.medium,
    Severity.low,
    Severity.info,
)

_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}


def severity_rank(sev: Severity) -> int:
    """0 = most severe (critical). Used to sort findings worst-first in auto_severity mode."""
    return _SEVERITY_RANK.get(sev, len(SEVERITY_ORDER))


def risk_rating(counts: dict[Severity, int]) -> Severity:
    """Overall risk = the most severe level that has at least one finding; info if none.

    Mirrors Lotek's ``risk_rating`` ladder (critical -> high -> ... -> info).
    """
    for sev in SEVERITY_ORDER:
        if counts.get(sev, 0) > 0:
            return sev
    return Severity.info
