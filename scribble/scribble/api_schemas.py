"""Declarative request schemas for Scribble's PAT machine API — schema source for the host's OpenAPI doc.

``request_body`` stamps the same conventional attribute the host generator reads
(``app.api_schemas.REQUEST_MODEL_ATTR``), spelled as a literal (an extension must not import a host).
Declarative only — the handlers keep their own lenient parsing and error contracts.
"""

from __future__ import annotations

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


class AddFindingRequest(BaseModel):
    """Body of ``POST /scribble/machine/engagements/{engagement_id}/findings`` (write scope).

    Provide ``template_id`` (instantiate a library vuln template) OR ``lotek_finding_id`` (promote a scan
    finding). At least one is required.
    """

    template_id: int | None = Field(None, description="VulnerabilityTemplate id to instantiate.")
    lotek_finding_id: int | None = Field(None, description="lotek scan finding id to promote.")
    group_id: int | None = Field(None, description="Optional FindingGroup id to nest under.")
    target_host: str | None = Field(None, description="Override target host on the authored finding.")
    target_port: int | None = Field(None, description="Override target port.")
    target_url: str | None = Field(None, description="Override target URL.")


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
    idempotency_key: str | None = Field(
        None, description="Dedup key; a retry with the same key returns the original artifact (200)."
    )
