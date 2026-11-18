"""Declarative request schemas for Pastel's PAT machine API — schema source for the host's OpenAPI doc.

``request_body`` stamps the same conventional attribute the host generator reads
(``app.api_schemas.REQUEST_MODEL_ATTR``), spelled as a literal (an extension must not import a host).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from pastel.models import MAX_BODY, MAX_TITLE

REQUEST_MODEL_ATTR = "__lotek_request_model__"


def request_body(model: type[BaseModel]):
    def deco(fn):
        setattr(fn, REQUEST_MODEL_ATTR, model)
        return fn

    return deco


class CreatePasteRequest(BaseModel):
    """Body of ``POST /pastel/machine/pastes`` (write scope).

    The paste is attributed to the TOKEN'S user. Access controls are optional and compose: a paste may
    be password-protected AND expiring AND read-limited at once. A runner-set ``password`` is hashed
    server-side; a machine reader later supplies it to the public GUID URL as HTTP Basic (any username,
    the password in the password field).
    """

    body: str = Field(..., max_length=MAX_BODY, description="The markdown paste content.")
    title: str | None = Field(None, max_length=MAX_TITLE, description="Optional one-line title.")
    password: str | None = Field(
        None, description="Optional: protect the GUID URL with HTTP Basic auth (stored hashed)."
    )
    expires_in: str | None = Field(
        None, description="Optional TTL: 1h | 6h | 1d | 7d | 30d, or an integer number of seconds."
    )
    expires_at: str | None = Field(
        None, description="Optional absolute ISO-8601 expiry (wins over expires_in if both are sent)."
    )
    max_reads: int | None = Field(
        None, ge=1, description="Optional: retire the paste after this many successful GUID fetches."
    )
