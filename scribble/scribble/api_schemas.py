"""Declarative request schemas for Scribble's PAT machine API — schema source for the host's OpenAPI doc.

``request_body`` stamps the same conventional attribute the host generator reads
(``app.api_schemas.REQUEST_MODEL_ATTR``), spelled as a literal (an extension must not import a host).
Declarative only — the handlers keep their own lenient parsing and error contracts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

REQUEST_MODEL_ATTR = "__lotek_request_model__"


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

    template_id: int | None = Field(None, description="VulnerabilityTemplate id to instantiate.")
    lotek_finding_id: int | str | None = Field(
        None, description="lotek scan finding id to promote (int or UUID — v2 keys findings on UUIDv7)."
    )
    group_id: int | None = Field(None, description="Optional FindingGroup id to nest under.")
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
    references: list[str] | None = Field(
        None, description="Optional reference URLs/text; wrapped into the 'references' content block."
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

    Prose can arrive either as plain text (``description``/``remediation``/``references``) or as
    ProseMirror ``content_json``; either way it is sanitized (allowlisted node/mark set) before persist,
    through the same path the create route uses.
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
    references: list[str] | None = Field(None, description="Reference URLs/text -> 'references' block.")
    content_json: dict[str, Any] | None = Field(
        None,
        description="{block_name: prosemirror_doc}; merged per block over the existing content and "
        "sanitized before persist. Blocks not mentioned are untouched.",
    )
    idempotency_key: str | None = Field(
        None, description="Dedup key (or Idempotency-Key header); a retry replays the original response."
    )


class MoveFindingRequest(BaseModel):
    """Body of ``POST /scribble/machine/findings/{finding_id}/move`` (write scope).

    ``group_id`` must be PRESENT — ``null`` means the ungrouped bucket. A group id that does not exist, or
    belongs to another engagement, is a 404 (never a silent drop: on a move the destination IS the
    request). Moving into a group switches that group to manual ordering.
    """

    group_id: int | None = Field(
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

    finding_ids: list[int] = Field(..., description="Findings to move, in the order they should land.")
    group_id: int | None = Field(
        ..., description="Destination FindingGroup id, or null for the ungrouped bucket. Required."
    )
    order_index: int | None = Field(0, description="Position of the FIRST moved finding (default 0).")
    idempotency_key: str | None = Field(None, description="Dedup key (or Idempotency-Key header).")


class CreateGroupRequest(BaseModel):
    """Body of ``POST /scribble/machine/engagements/{engagement_id}/groups`` (write scope) — a report
    section, appended last on the board."""

    name: str = Field(..., description="Section name (required).")
    assessment_type_id: int | None = Field(
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

    order: list[int] = Field(..., description="Group ids in their new top-to-bottom order.")
    idempotency_key: str | None = Field(None, description="Dedup key (or Idempotency-Key header).")


class UploadArtifactRequest(BaseModel):
    """JSON body of ``POST /scribble/machine/engagements/{engagement_id}/artifacts`` (write scope).

    The endpoint ALSO accepts a ``multipart/form-data`` upload with a ``file`` field; this schema
    documents the JSON/base64 variant an agent typically uses to attach a screenshot or evidence file.
    """

    filename: str = Field(..., description="File name (its extension guides the content type).")
    content_base64: str = Field(..., description="Base64-encoded file bytes (aliases: data_base64, data).")
    finding_id: int | None = Field(None, description="Attach the artifact to this engagement finding.")
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
