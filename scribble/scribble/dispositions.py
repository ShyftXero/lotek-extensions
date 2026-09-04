"""The ``FindingDTO`` -> ``EngagementFinding`` disposition contract (map #616, decision #617).

``EngagementFinding`` is a LOSSLESS SUPERSET of the scan ``FindingDTO`` (``host_contract.FindingDTO``,
the neutral seam scribble consumes -- scribble never imports lotek). Every DTO field carries an explicit
DISPOSITION on independent axes:

  * **home** -- where the field lives on the ``EngagementFinding``:
      ``column``        a typed column (the value is ALSO in ``source_facts`` verbatim)
      ``source_facts``  only in the verbatim ``source_facts`` snapshot (no typed column yet)
      ``drop``          intentionally not represented at all (reasoned; none today)
  * **origin** -- how the value arrives: ``promote`` (copied from the scan DTO), ``author`` (created by
      a human later), ``enrichment`` (added by an offline pass).
  * **operator** -- who owns it after: ``locked`` (system-owned) or ``editable`` (an operator may edit;
      re-promote must NEVER clobber an operator edit -- #617 Q5, fill-NULL-only).

``source_facts`` captures the WHOLE DTO verbatim at promote time, so losslessness is STRUCTURAL rather
than per-field-hopeful. The registry below is therefore mainly a DRIFT GUARD: it forces every DTO field
to carry a considered disposition (typed column vs snapshot-only vs dropped), proven by
``tests/test_finding_dto_disposition_drift.py`` (the pattern lotek core's
``test_runner_reachability_single_source.py`` established).

Downstream tickets promote ``source_facts`` fields to typed columns against THIS rule: references (#624),
cve/cwe/owasp (#625), retest fields (#621), export (#627), attack-chain links (#628). Widening the DTO
itself to carry a currently-dropped core field is out of scope until one earns a report use (#617).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Any

from scribble.enums import Confidence, FindingStatus


class Home(enum.StrEnum):
    column = "column"              # a typed EngagementFinding column (also in source_facts)
    source_facts = "source_facts"  # only in the verbatim snapshot; no typed column yet
    drop = "drop"                  # intentionally unrepresented (reasoned; none today)


class Origin(enum.StrEnum):
    promote = "promote"
    author = "author"
    enrichment = "enrichment"


class Operator(enum.StrEnum):
    locked = "locked"
    editable = "editable"


@dataclass(frozen=True)
class Disposition:
    """One DTO field's home + two axes. ``column`` is the ``EngagementFinding`` column when
    ``home is Home.column`` (several prose fields legitimately share ``content_json``), else ``None``."""

    field: str
    home: Home
    origin: Origin
    operator: Operator
    column: str | None
    note: str = ""


# One entry per FindingDTO field (host_contract.FindingDTO). Kept in DTO declaration order for review.
DISPOSITIONS: tuple[Disposition, ...] = (
    Disposition("id", Home.column, Origin.promote, Operator.locked, "source_finding_id",
                "core Finding id -> the dedup/provenance soft-ref"),
    Disposition("job_id", Home.source_facts, Origin.promote, Operator.locked, None,
                "scan-job provenance; no report use beyond the snapshot"),
    Disposition("title", Home.column, Origin.promote, Operator.editable, "title", ""),
    Disposition("category", Home.column, Origin.promote, Operator.editable, "category", ""),
    Disposition("source", Home.source_facts, Origin.promote, Operator.locked, None,
                "producing tool; drives ScribbleVulnMap resolution, not stored as a column"),
    Disposition("severity", Home.column, Origin.promote, Operator.editable, "severity", ""),
    Disposition("confidence", Home.column, Origin.promote, Operator.editable, "confidence",
                "mapped in promote (#617); previously defaulted silently to medium"),
    Disposition("status", Home.column, Origin.promote, Operator.editable, "status",
                "mapped in promote (#617); previously defaulted silently to new"),
    Disposition("dedupe_key", Home.source_facts, Origin.promote, Operator.locked, None, ""),
    Disposition("description", Home.column, Origin.promote, Operator.editable, "content_json",
                "prose -> content_json['description'] block"),
    Disposition("remediation", Home.column, Origin.promote, Operator.editable, "content_json",
                "prose -> content_json['remediation'] block"),
    Disposition("references", Home.source_facts, Origin.promote, Operator.editable, None,
                "typed column deferred to #624 (union of template + scan refs, suppressable)"),
    Disposition("cve", Home.source_facts, Origin.promote, Operator.editable, None,
                "typed cve_ids column deferred to #625"),
    Disposition("cvss_score", Home.column, Origin.promote, Operator.editable, "cvss_score", ""),
    Disposition("analyst_notes", Home.column, Origin.promote, Operator.editable, "analyst_notes", ""),
    Disposition("evidence", Home.column, Origin.promote, Operator.editable, "content_json",
                "raw evidence -> content_json['details'] as a last resort; verbatim in source_facts"),
    Disposition("asset_identifier", Home.source_facts, Origin.promote, Operator.locked, None,
                "informs target_host via host derivation; the identifier itself is not stored"),
    Disposition("target_host", Home.column, Origin.promote, Operator.editable, "target_host", ""),
    Disposition("facts", Home.source_facts, Origin.promote, Operator.locked, None,
                "declared-facts dict; drives the variables overlay via resolve_variables"),
)

#: The DTO field names the registry covers -- the canonical boundary, single-sourced from DISPOSITIONS.
FINDING_DTO_FIELDS: frozenset[str] = frozenset(d.field for d in DISPOSITIONS)

#: Typed columns an operator may edit -- re-promote must never overwrite a non-empty one (#617 Q5).
EDITABLE_COLUMNS: frozenset[str] = frozenset(
    d.column for d in DISPOSITIONS
    if d.home is Home.column and d.operator is Operator.editable and d.column is not None
)


def _jsonable(value: Any) -> Any:
    """A JSON-serializable form of a DTO value for the ``source_facts`` column. ``DTO.id`` may be a
    ``uuid.UUID`` (JSON has no UUID type); dicts/lists are coerced recursively; an unrecognised object
    degrades to ``str`` rather than raising at ``json.dumps`` time on commit."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def snapshot_source_facts(dto: Any) -> dict[str, Any]:
    """The full ``FindingDTO`` captured VERBATIM as a JSON-safe dict -- one key per registry field, read
    by ``getattr`` so it works on the real ``host_contract.FindingDTO``, a test fake, or anything
    duck-shaped (promote never imports lotek). A missing attribute is recorded as ``None`` so the
    snapshot's key set is STABLE (every registry field is present) and losslessness is structural."""
    return {d.field: _jsonable(getattr(dto, d.field, None)) for d in DISPOSITIONS}


def confidence_from_dto(dto: Any, default: Confidence = Confidence.medium) -> Confidence:
    """Map ``DTO.confidence`` (a plain value string, or an enum-shaped one) to scribble's ``Confidence``.
    DEFENSIVE: an unknown/absent value degrades to ``default`` rather than raising -- a scan must never
    break a promote, and the harness is no kinder than prod (mirrors ``from_lotek_finding``'s severity
    normalization). Core's ``Confidence`` values equal scribble's exactly today, so a valid value maps
    1:1; the guard is for a future host that emits a value scribble does not know."""
    raw = getattr(dto, "confidence", None)
    raw = getattr(raw, "value", raw)
    try:
        return Confidence(raw)
    except (ValueError, TypeError):
        return default


def status_from_dto(dto: Any, default: FindingStatus = FindingStatus.new) -> FindingStatus:
    """Map ``DTO.status`` to scribble's ``FindingStatus``; unknown/absent -> ``default``. Defensive for
    the same reason as ``confidence_from_dto``."""
    raw = getattr(dto, "status", None)
    raw = getattr(raw, "value", raw)
    try:
        return FindingStatus(raw)
    except (ValueError, TypeError):
        return default
