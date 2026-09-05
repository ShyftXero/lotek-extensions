"""Declarative request schemas for Scribble's PAT machine API — schema source for the host's OpenAPI doc.

``request_body`` stamps the same conventional attribute the host generator reads
(``app.api_schemas.REQUEST_MODEL_ATTR``), spelled as a literal (an extension must not import a host).
Declarative only — the handlers keep their own lenient parsing and error contracts.

🔴 **An id field's declared TYPE is part of the published contract, not decoration.** These models are
what `components.schemas` is generated from, in both lotek's `/api/v1/openapi.json` and scribble's own
`/scribble/machine/openapi.json`, so a client is written from them. They went stale at the UUIDv7
migration (#36 / lotek#335) and kept declaring `integer` for eight fields the handlers parse with
``_opt_uuid`` — a generated client sending ``{"template_id": 1}`` 400s on every call, which is exactly
the silent-wrong-guess failure #116 was filed about, published with authority. Two rules:

* A **scribble-owned** id (template, group, finding, artifact, assessment type) is ``uuid.UUID``.
* A **host (core) reference** stays ``int | str`` — ``client_id``, ``core_engagement_id`` and
  ``lotek_finding_id`` are ``SoftHostId``s that are ints on a legacy/standalone host and UUIDs under
  lotek v2, and narrowing them to UUID would refuse a legitimate caller.

``tests/test_machine_openapi.py::test_request_model_ids_are_uuid_typed`` enforces exactly that split.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

REQUEST_MODEL_ATTR = "__lotek_request_model__"

# Stamped onto a view that routes its mutation through ``api_pat._with_idempotency``, so the OpenAPI
# generator can say which operations really honour ``Idempotency-Key`` instead of guessing from the HTTP
# method. It guessed, and the guess was wrong for four routes — including ``promote-job``, which
# BULK-CREATES findings and has no idempotency at all. Advertising retry-safety a route does not have is
# worse than advertising nothing, because a client retries on that promise. Same mechanism as
# ``host.SCOPE_ATTR``; it lives here rather than in ``api_pat`` so ``openapi.py`` can read it without an
# import cycle.
IDEMPOTENT_ATTR = "__scribble_idempotent__"


def idempotent_route(fn):
    """Mark a view as honouring ``Idempotency-Key`` through the host seam. Declarative only — the actual
    behaviour is ``api_pat._with_idempotency``; this just makes it visible to the document generator."""
    setattr(fn, IDEMPOTENT_ATTR, True)
    return fn


def request_body(model: type[BaseModel]):
    def deco(fn):
        setattr(fn, REQUEST_MODEL_ATTR, model)
        return fn

    return deco


class CreateEngagementRequest(BaseModel):
    """Body of ``POST /scribble/machine/engagements`` — create a report engagement (write scope)."""

    name: str = Field(..., description="Engagement name (required).")
    client_id: int | str | None = Field(
        None,
        description="Host client id (int or UUID). REQUIRED when mounted in a host — a mounted engagement "
        "is scoped by its client, and one with no client is readable by nobody.",
    )
    scope_type: str | None = Field("external", description="e.g. 'external' | 'internal' (default external).")
    company_name: str | None = Field(None, description="Optional company/customer display name.")
    core_engagement_id: int | str | None = Field(
        None,
        description="Core engagement id (int or UUID) this scribble engagement mirrors — lets a PAT "
        "caller later address it by the id core returned; surfaced in GET /engagements output.",
    )
    idempotency_key: str | None = Field(
        None,
        description="Dedup key (or Idempotency-Key header). A retry with the SAME request replays the "
        "original response; the same key with a DIFFERENT request is refused 422 (use a new key).",
    )


class AddFindingRequest(BaseModel):
    """Body of ``POST /scribble/machine/engagements/{engagement_id}/findings`` (write scope).

    THREE mutually-exclusive authoring modes, tried in this order:
      * ``template_id`` — instantiate a library vuln template;
      * ``lotek_finding_id`` — promote a scan finding;
      * neither — AUTHOR a finding directly from ``title`` + ``severity`` (+ the optional prose/CVSS/
        reference/target fields below). ``title`` and ``severity`` are then required.

    Any supplied ``content_json`` is sanitized (allowlisted ProseMirror node/mark set) before it is
    persisted — a write-scoped PAT cannot store markup that would execute when the report is opened.
    """

    template_id: uuid.UUID | None = Field(None, description="VulnerabilityTemplate id to instantiate.")
    lotek_finding_id: int | str | None = Field(
        None, description="lotek scan finding id to promote (int or UUID — v2 keys findings on UUIDv7)."
    )
    group_id: uuid.UUID | None = Field(None, description="Optional FindingGroup id to nest under.")
    target_host: str | None = Field(None, description="Override target host on the authored finding.")
    target_port: int | None = Field(None, description="Override target port.")
    target_url: str | None = Field(None, description="Override target URL.")
    # ── direct-authoring branch (no template_id / no lotek_finding_id) ──
    title: str | None = Field(None, description="Finding title (required when authoring directly).")
    severity: str | None = Field(
        None, description="info | low | medium | high | critical (required when authoring directly)."
    )
    description: str | None = Field(
        None, description="Plain-text description; wrapped into the 'description' content block."
    )
    remediation: str | None = Field(
        None, description="Plain-text remediation; wrapped into the 'remediation' content block."
    )
    cvss_vector: str | None = Field(None, description="Optional CVSS vector string.")
    references: list[Any] | None = Field(
        None,
        description="Structured references (#624) -> the typed EngagementFinding.references column. Each "
        "element is a URL/label string, or a {label, url, source, suppressed} object. Rendered as an "
        "omit-when-empty labeled-link block (non-suppressed only), NOT a prose content block.",
    )
    content_json: dict[str, Any] | None = Field(
        None,
        description="Optional {block_name: prosemirror_doc} content, sanitized before persist. Supersedes "
        "the plain-text description/remediation for any block it supplies.",
    )
    idempotency_key: str | None = Field(
        None,
        description="Dedup key (or Idempotency-Key header). A retry with the SAME request replays the "
        "original response; the same key with a DIFFERENT request is refused 422 (use a new key).",
    )


class CreateTemplateRequest(BaseModel):
    """Body of ``POST /scribble/machine/templates`` — create a reusable vuln template (write scope)."""

    name: str = Field(..., description="Template name (required).")
    category: str | None = Field(None, description="Optional category label.")
    default_severity: str = Field(
        "medium", description="info | low | medium | high | critical (default medium)."
    )
    description: str | None = Field(
        None, description="Plain-text description; packed into the 'description' content block."
    )
    remediation: str | None = Field(
        None, description="Plain-text remediation; packed into the 'remediation' content block."
    )
    references: list[str] | None = Field(None, description="Optional reference URLs/text.")
    content_json: dict[str, Any] | None = Field(
        None,
        description="Optional ProseMirror content blocks ({block_name: doc}). Sanitized (allowlisted "
        "node/mark set) before persist. When omitted, 'description'/'remediation' are packed into "
        "content blocks from the plain-text fields above.",
    )
    idempotency_key: str | None = Field(
        None,
        description="Dedup key (or Idempotency-Key header). A retry with the SAME request replays the "
        "original response; the same key with a DIFFERENT request is refused 422 (use a new key).",
    )


class PatchFindingRequest(BaseModel):
    """Body of ``PATCH /scribble/machine/findings/{finding_id}`` (write scope) — partial edit.

    Only the fields present in the body change; an explicit ``null`` CLEARS a nullable column, and an
    omitted field is left alone. UNKNOWN field names are refused (400) rather than ignored: a typo would
    otherwise return 200 for an edit that never happened. ``group_id``/``order_index`` are deliberately
    NOT here — re-ordering and grouping belong to ``POST /findings/{finding_id}/move``.

    Prose can arrive either as plain text (``description``/``remediation``) or as ProseMirror
    ``content_json``; either way it is sanitized (allowlisted node/mark set) before persist, through the
    same path the create route uses. ``references`` and the CVE/CWE/OWASP metadata are STRUCTURED typed
    columns (#624/#625), not prose blocks — see the per-field descriptions below.
    """

    title: str | None = Field(None, description="New title (must be non-empty when supplied).")
    severity: str | None = Field(None, description="info | low | medium | high | critical.")
    confidence: str | None = Field(None, description="low | medium | high.")
    status: str | None = Field(
        None, description="new | triaged | accepted_risk | false_positive | fixed | needs_retest."
    )
    category: str | None = Field(None, description="Category label; null clears it.")
    cvss_score: float | None = Field(None, description="CVSS numeric score; null clears it.")
    cvss_vector: str | None = Field(None, description="CVSS vector string; null clears it.")
    target_host: str | None = Field(None, description="Target host; null clears it.")
    target_port: int | str | None = Field(None, description="Target port; null clears it.")
    target_url: str | None = Field(None, description="Target URL; null clears it.")
    analyst_notes: str | None = Field(None, description="Internal analyst notes; null clears it.")
    include_in_report: bool | None = Field(None, description="Whether the finding renders in the report.")
    description: str | None = Field(None, description="Plain-text prose for the 'description' block.")
    remediation: str | None = Field(None, description="Plain-text prose for the 'remediation' block.")
    references: list[Any] | None = Field(
        None,
        description="Structured references (#624) -> the typed EngagementFinding.references column. Each "
        "element is a URL/label string or a {label, url, source, suppressed} object; supply the full list "
        "to add/edit, set an element's suppressed=true to hide it. Author-added refs are source=author.",
    )
    cve_ids: list[str] | None = Field(
        None, description="CVE ids (#625), normalized to CVE-YYYY-NNNN and deduped; replaces the list."
    )
    cwe_ids: list[str] | None = Field(
        None, description="CWE ids (#625), normalized to CWE-NNN and deduped; replaces the list."
    )
    owasp_categories: list[str] | None = Field(
        None,
        description="OWASP Top-10-2021 category ids (#625), e.g. 'A03:2021'; unknown ids are dropped. "
        "Normally DERIVED from cwe_ids at promote time — this is the author override.",
    )
    threat_intel: dict | None = Field(
        None,
        description="Dated KEV/EPSS snapshot (#625). Enrichment-managed: send null to CLEAR it; a non-null "
        "value is refused (it is refreshed by the enrichment pass keyed to the finding's CVEs, not "
        "hand-authored — a hand-typed KEV/EPSS with no honest as_of is a stale fact).",
    )
    content_json: dict[str, Any] | None = Field(
        None,
        description="{block_name: prosemirror_doc}; merged per block over the existing content and "
        "sanitized before persist. Blocks not mentioned are untouched.",
    )
    idempotency_key: str | None = Field(
        None, description="Dedup key (or Idempotency-Key header); a retry replays the original response."
    )


class PatchEngagementRequest(BaseModel):
    """Body of ``PATCH /scribble/machine/engagements/{engagement_id}`` (write scope) — lotek#620.

    Sets or clears the MANUAL override of the report's computed overall-risk band. Only fields present
    change; an omitted field is left alone and an explicit ``null`` CLEARS. Setting ``risk_override``
    REQUIRES a non-empty ``risk_override_rationale`` (400 otherwise) — an unreasoned override would read
    as a computed fact, which the report must never do. Clearing the override clears its rationale. The
    computed ``risk_rating`` ladder is never destroyed: the override is an authored judgement layered on
    top and rendered with an "assessor-adjusted" marker beside the original computed band.
    """

    risk_override: str | None = Field(
        None,
        description="info | low | medium | high | critical — overrides the computed overall band; an "
        "explicit null clears the override (and its rationale). Direction is unrestricted (up or down).",
    )
    risk_override_rationale: str | None = Field(
        None,
        description="Assessor's reason for the override — REQUIRED (non-empty) whenever risk_override is "
        "set; rendered verbatim beside the adjusted rating. null/empty clears it.",
    )
    strategic_recommendations: list[str] | None = Field(
        None,
        description="lotek#623: authored strategic (longer-horizon) recommendations, rendered as the "
        "report's Strategic Recommendations section. Blanks/non-strings are dropped; null or [] clears "
        "the list. Omitted = unchanged.",
    )


class MoveFindingRequest(BaseModel):
    """Body of ``POST /scribble/machine/findings/{finding_id}/move`` (write scope).

    ``group_id`` must be PRESENT — ``null`` means the ungrouped bucket. A group id that does not exist, or
    belongs to another engagement, is a 404 (never a silent drop: on a move the destination IS the
    request). Moving into a group switches that group to manual ordering.
    """

    group_id: uuid.UUID | None = Field(
        ..., description="Destination FindingGroup id, or null for the ungrouped bucket. Required."
    )
    order_index: int | None = Field(
        0, description="Position within the destination, as rendered (default 0 = first)."
    )
    idempotency_key: str | None = Field(None, description="Dedup key (or Idempotency-Key header).")


class BulkMoveFindingsRequest(BaseModel):
    """Body of ``POST /scribble/machine/engagements/{engagement_id}/findings/move`` (write scope).

    The multi-select form: move several findings into one group in a single call, preserving the listed
    order (each lands at ``order_index`` + its position). ATOMIC — if any id is not in this engagement the
    whole request is refused (404) and nothing moves.
    """

    finding_ids: list[uuid.UUID] = Field(
        ..., description="Findings to move, in the order they should land."
    )
    group_id: uuid.UUID | None = Field(
        ..., description="Destination FindingGroup id, or null for the ungrouped bucket. Required."
    )
    order_index: int | None = Field(0, description="Position of the FIRST moved finding (default 0).")
    idempotency_key: str | None = Field(None, description="Dedup key (or Idempotency-Key header).")


class CreateGroupRequest(BaseModel):
    """Body of ``POST /scribble/machine/engagements/{engagement_id}/groups`` (write scope) — a report
    section, appended last on the board."""

    name: str = Field(..., description="Section name (required).")
    assessment_type_id: uuid.UUID | None = Field(
        None, description="Optional library AssessmentType to link; an unknown id is left unset."
    )
    idempotency_key: str | None = Field(None, description="Dedup key (or Idempotency-Key header).")


class UpdateGroupRequest(BaseModel):
    """Body of ``PATCH /scribble/machine/engagements/{engagement_id}/groups/{group_id}`` (write scope).

    Partial: supply only what changes. ``order_mode="auto_severity"`` is the way back from the manual
    ordering that any move flips a group into ("re-rank by severity").
    """

    name: str | None = Field(None, description="New section name (must be non-empty when supplied).")
    order_mode: str | None = Field(None, description="auto_severity | manual.")
    include_in_report: bool | None = Field(None, description="Whether the section renders in the report.")
    idempotency_key: str | None = Field(None, description="Dedup key (or Idempotency-Key header).")


class ReorderGroupsRequest(BaseModel):
    """Body of ``POST /scribble/machine/engagements/{engagement_id}/groups/reorder`` (write scope).

    Stale, foreign or duplicated ids are ignored, and any section the payload omits keeps its relative
    order at the end — a partial payload never drops a section.
    """

    order: list[uuid.UUID] = Field(..., description="Group ids in their new top-to-bottom order.")
    idempotency_key: str | None = Field(None, description="Dedup key (or Idempotency-Key header).")


class UploadArtifactRequest(BaseModel):
    """JSON body of ``POST /scribble/machine/engagements/{engagement_id}/artifacts`` (write scope).

    The endpoint ALSO accepts a ``multipart/form-data`` upload with a ``file`` field; this schema
    documents the JSON/base64 variant an agent typically uses to attach a screenshot or evidence file.
    """

    filename: str = Field(..., description="File name (its extension guides the content type).")
    content_base64: str = Field(..., description="Base64-encoded file bytes (aliases: data_base64, data).")
    finding_id: uuid.UUID | None = Field(None, description="Attach the artifact to this engagement finding.")
    caption: str | None = Field(None, description="Human caption shown in the report.")
    kind: str | None = Field(None, description="'screenshot' | 'text' | 'file' (inferred if omitted).")
    placement: str | None = Field(None, description="'attached' (default) | 'inline'.")
    include_in_report: bool | None = Field(
        None,
        description="Whether this evidence appears in the rendered report (default true). An artifact with "
        "no finding_id is published in the report's engagement-level Evidence appendix, so send false for "
        "working material you are attaching but do not want in the client deliverable.",
    )
    idempotency_key: str | None = Field(
        None, description="Dedup key; a retry with the same key returns the original artifact (200)."
    )


class UpdateArtifactRequest(BaseModel):
    """JSON body of ``POST /scribble/machine/engagements/{engagement_id}/artifacts/{artifact_id}``
    (write scope) — change whether one artifact ships, and/or its caption. Omitted fields are unchanged.
    """

    include_in_report: bool | None = Field(
        None, description="Publish (true) or withhold (false) this artifact in the rendered report."
    )
    caption: str | None = Field(None, description="Human caption shown in the report.")


class UpdateAttackPathRequest(BaseModel):
    """JSON body of ``PATCH /scribble/machine/engagements/{engagement_id}/attack-paths/{attack_path_id}``
    (write scope) — edit a linked attack path in place. Omitted fields are unchanged; an UNKNOWN field is
    a 400, not a silent no-op.

    ``include_in_report: false`` is the NON-destructive way to keep a wrongly-linked diagram out of a
    client deliverable — reach for it before ``DELETE``, which also destroys the stored snapshot.
    """

    include_in_report: bool | None = Field(
        None,
        description="Publish (true) or withhold (false) this diagram in the rendered report. Must be a "
        "real boolean when present — an explicit null is a 400, not 'unchanged'.",
    )
    caption: str | None = Field(None, description="Human caption shown under the diagram in the report.")
    idempotency_key: str | None = Field(
        None,
        description="Dedup key (or Idempotency-Key header). A retry with the SAME request replays the "
        "original response; the same key with a DIFFERENT request is refused 422 (use a new key).",
    )


class LinkAttackPathRequest(BaseModel):
    """JSON body of ``POST /scribble/machine/engagements/{engagement_id}/attack-paths`` (write scope) —
    link a vector attack-path diagram into this engagement's report (ext#48).

    Scribble cannot reach vector directly (separate extension, no host seam exposes it), so the caller
    does the fetch: ``GET`` vector's ``/vector/machine/diagrams/{diagram_id}/export.html`` (already a
    self-contained document — inline assets, no external references) and POST the resulting HTML here
    as ``embed_html``. The report renders it inside a sandboxed iframe (``allow-scripts`` only); this
    endpoint stores it verbatim and does not parse or execute it.
    """

    diagram_ref: str | None = Field(
        None, description="The source vector diagram's id/UUID, kept for provenance/dedup only."
    )
    embed_html: str = Field(
        ..., description="Self-contained HTML snapshot (vector's export.html) to embed in the report."
    )
    caption: str | None = Field(None, description="Human caption shown under the diagram in the report.")
    include_in_report: bool | None = Field(
        None, description="Whether this diagram appears in the rendered report (default true)."
    )
    idempotency_key: str | None = Field(
        None, description="Dedup key; a retry with the same key returns the original link (200)."
    )
