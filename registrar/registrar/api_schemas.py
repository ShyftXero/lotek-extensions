"""Declarative request schemas for Registrar's PAT machine API — schema source for the host's OpenAPI doc.

``request_body`` stamps the same conventional attribute the host generator reads
(``app.api_schemas.REQUEST_MODEL_ATTR``), spelled as a literal (an extension must not import a host).
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


class ActionRequest(BaseModel):
    """Body of ``POST /registrar/machine/action`` (write scope).

    Direct-tier verbs run inline. Confirm-tier verbs (outward effect) are STAGED only (202) — they can
    NEVER execute via the machine API. Approval requires an interactive dashboard session by a different
    user (INV-EXT-02), so an agent on a PAT stages, and a human approves.
    """

    verb: str = Field(
        ...,
        description="Registrar driver verb. Direct-tier (run inline): 'list_nodes', 'list_records'. "
        "Confirm-tier (staged only): 'create_node', 'destroy_node', 'upsert_record', 'register_domain', "
        "'send_sms'. An unrecognized verb is treated as confirm-tier and staged, never executed.",
    )
    provider: str | None = Field("null", description="Provider slug; 'null' for the no-op provider.")
    args: dict[str, Any] | None = Field(None, description="Verb arguments.")
