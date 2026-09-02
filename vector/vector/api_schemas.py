"""Declarative request schemas for Vector's PAT machine API — the schema source for the host's generated
OpenAPI doc.

The ``request_body`` decorator stamps the SAME conventional attribute name the host generator reads
(``app.api_schemas.REQUEST_MODEL_ATTR``); spelled as a literal here because an extension must not import a
host module. Declarative only — the machine handlers keep their own parsing.
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


class CreateDiagramRequest(BaseModel):
    """Body of ``POST /vector/machine/diagrams`` — create an attack-path diagram (write scope)."""

    model_config = {"protected_namespaces": ()}

    name: str | None = Field(None, description="Diagram name; defaults to 'Untitled attack path'.")
    model: Any = Field(None, description="A vector.attackpath/v1 document; normalized before storage.")
    engagement_id: str | None = Field(
        None,
        description="Optional engagement (UUID) to bind this diagram to. If set, the token must hold an "
        "operator capability on it, and thereafter only live members of that engagement may read/export "
        "it and only operators may modify it — a revoked member loses access. Omit for an unbound, "
        "owner-scoped diagram.",
    )


class UpdateDiagramRequest(BaseModel):
    """Body of ``PUT /vector/machine/diagrams/{diagram_id}`` — update name and/or model (write scope)."""

    model_config = {"protected_namespaces": ()}

    name: str | None = Field(None, description="New name (omit to leave unchanged).")
    model: Any = Field(None, description="New vector.attackpath/v1 document (omit to leave unchanged).")
