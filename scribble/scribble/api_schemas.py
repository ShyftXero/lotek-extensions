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
