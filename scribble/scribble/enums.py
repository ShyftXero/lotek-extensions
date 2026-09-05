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


# ── report disposition: what a finding's STATUS means for the deliverable (lotek#618) ────────────
#
# THE one home for this derivation. Every surface -- ``reporting/context.py``, ``render_html``,
# ``render_docx``, and the board UI -- calls ``report_disposition`` rather than comparing ``status``
# itself, because two copies of this map do not stay equal and nothing raises when they disagree: a
# finding card reading "Remediated" while the risk banner still counts it as live is precisely that
# drift. It lives beside ``risk_rating`` for the same reason ``risk_rating`` lives here -- this module
# is the shared vocabulary every one of those callers already imports, so no new import edge is needed.
#
# It sits BESIDE ``include_in_report``, which stays the operator's explicit veto. Inclusion is
# ``include_in_report AND disposition != "excluded"``, ANDed once in :func:`report_visible` below.

#: A finding still stands: it renders AND counts toward the severity ladder.
DISPOSITION_LIVE = "live"
#: Recorded as fixed. It renders (the client should see it was addressed) but must NOT drive the
#: overall risk rating -- a remediated finding is not present risk.
DISPOSITION_REMEDIATED = "remediated"
#: The risk was accepted rather than fixed. Renders, also out of the ladder.
DISPOSITION_ACCEPTED = "accepted"
#: Not a real finding. Leaves the client deliverable entirely (see lotek#618 Decision 4: no
#: "excluded findings" annex -- a false positive is not a finding, and listing it invites exactly the
#: "undermining the triager" failure the reporting methodology warns about).
DISPOSITION_EXCLUDED = "excluded"

#: Every disposition, in report order. A rollup reports a count for each, so a reader can see the
#: shape of the deliverable without inferring it from what is missing.
DISPOSITIONS = (
    DISPOSITION_LIVE,
    DISPOSITION_REMEDIATED,
    DISPOSITION_ACCEPTED,
    DISPOSITION_EXCLUDED,
)

_DISPOSITION_BY_STATUS = {
    FindingStatus.new: DISPOSITION_LIVE,
    FindingStatus.triaged: DISPOSITION_LIVE,
    FindingStatus.needs_retest: DISPOSITION_LIVE,
    FindingStatus.fixed: DISPOSITION_REMEDIATED,
    FindingStatus.accepted_risk: DISPOSITION_ACCEPTED,
    FindingStatus.false_positive: DISPOSITION_EXCLUDED,
}

# The CLIENT-FACING label. The internal vocabulary (`accepted_risk`, `needs_retest`) is not what a
# deliverable prints, and the wording is deliberately weaker than it could be: "Remediated" rather
# than "Fixed (verified)", "Risk accepted" rather than "Accepted risk (client decision)". Both of the
# stronger forms assert work nobody recorded -- a verification, a client sign-off -- which is the
# standing constraint `tests/test_report_standing_prose.py` pins for the report's prose. Verification
# wording belongs to the retest model (lotek#621), where a date and a verifier exist.
#
# `new` maps to NO label on purpose: a badge on every untriaged finding is noise, and it reads to a
# client as "draft". An engagement where every finding is `new` therefore renders exactly as it did
# before this feature existed.
_STATUS_LABEL = {
    FindingStatus.new: "",
    FindingStatus.triaged: "Triaged",
    FindingStatus.needs_retest: "Awaiting retest",
    FindingStatus.fixed: "Remediated",
    FindingStatus.accepted_risk: "Risk accepted",
    FindingStatus.false_positive: "",
}


def coerce_finding_status(
    status: FindingStatus | str | None, default: FindingStatus | None = None
) -> FindingStatus:
    """Coerce whatever a caller holds to a ``FindingStatus``; an unknown value reads as ``default``.

    A row loaded through the ORM yields the enum, but a value that has crossed a JSON boundary (the
    machine API, a host ``FindingDTO``) is a plain string. Unknown -> ``new`` keeps this fail-SAFE: an
    unrecognised status leaves the finding live and counted, so a vocabulary the report does not
    understand can never quietly drop a real finding out of a client deliverable.

    PUBLIC, and the single home for that rule, because there were briefly two: this function (read
    side, deciding what a status means for the report) and ``dispositions.status_from_dto`` (write
    side, promote). "Unknown status -> safe default" is a correctness rule, not a formatting detail —
    two copies that disagree would have promote store one thing and the report interpret another, with
    nothing raising. The drift guard in ``tests/test_report_disposition_single_source.py`` is what
    caught the second copy when #617's `dispositions.py` landed.
    """
    if isinstance(status, FindingStatus):
        return status
    try:
        return FindingStatus(str(status))
    except ValueError:
        return default if default is not None else FindingStatus.new


def report_disposition(status: FindingStatus | str | None) -> str:
    """What ``status`` means for the deliverable: one of :data:`DISPOSITIONS`."""
    return _DISPOSITION_BY_STATUS[coerce_finding_status(status)]


def finding_status_label(status: FindingStatus | str | None) -> str:
    """The client-facing label for ``status``; empty when nothing should be shown."""
    return _STATUS_LABEL[coerce_finding_status(status)]


def counts_toward_risk(status: FindingStatus | str | None) -> bool:
    """Does a finding with this status drive the overall risk rating? Only a live one does."""
    return report_disposition(status) == DISPOSITION_LIVE


def report_visible(finding) -> bool:
    """Does this finding reach the deliverable at all? Decided HERE, once, and nowhere else.

    Two independent reasons a finding is out, ANDed (lotek#618):
      * ``include_in_report`` -- the operator's EXPLICIT veto, unchanged;
      * a disposition of ``excluded`` -- derived from ``status`` (a ``false_positive`` is not a
        finding, so it leaves the client deliverable entirely).

    Every finding-level filter site calls this rather than re-deriving either half, and
    ``tests/test_report_disposition_single_source.py`` sweeps the package's AST to keep it that way.
    It is not a hypothetical: this predicate was born in ``reporting/context.py`` claiming to be the
    only home while ``findings_service.rendered_top_level_count`` -- the ``top_level_count`` the
    machine API publishes as "what the client sees" -- still filtered on ``include_in_report`` alone
    and over-reported the deliverable by exactly the number of false positives.

    It lives in ``enums.py`` rather than in ``reporting/context.py`` for an import-direction reason:
    ``context`` imports ``findings_service`` (for the nesting rule), so ``findings_service`` cannot
    import back without a cycle. ``enums`` is the shared vocabulary BOTH already import, so the one
    copy needs no new edge -- the same argument ``report_disposition`` above is here on.
    """
    return bool(finding.include_in_report) and report_disposition(finding.status) != DISPOSITION_EXCLUDED
