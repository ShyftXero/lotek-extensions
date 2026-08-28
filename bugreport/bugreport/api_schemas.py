"""Declarative request schemas for Bugreport's PAT machine API — schema source for the host's OpenAPI doc.

``request_body`` stamps the same conventional attribute the host generator reads
(``app.api_schemas.REQUEST_MODEL_ATTR``), spelled as a literal (an extension must not import a host).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from bugreport.models import MAX_BODY, MAX_TITLE

REQUEST_MODEL_ATTR = "__lotek_request_model__"


def request_body(model: type[BaseModel]):
    def deco(fn):
        setattr(fn, REQUEST_MODEL_ATTR, model)
        return fn

    return deco


class CreateReportRequest(BaseModel):
    """Body of ``POST /bugreport/machine/reports`` (write scope). Text only.

    The report is attributed to the TOKEN'S user — there is no reporter field, because a caller-supplied
    one would let a token file under someone else's name.
    """

    title: str = Field(..., max_length=MAX_TITLE, description="One line: what is broken.")
    body: str | None = Field(
        None, max_length=MAX_BODY, description="Free text: what you did, saw, and expected."
    )


class UpdateReportRequest(BaseModel):
    """Body of ``PATCH /bugreport/machine/reports/<id>`` (write scope).

    ``title``/``body`` are the REPORTER's edit of their own report. ``status``/``note`` are the ADMIN's
    response and are refused for anyone else. Sending both in one call is refused — they are two
    different authorizations, and one body that spans both is how the weaker one gets used to smuggle
    the stronger.
    """

    title: str | None = Field(None, max_length=MAX_TITLE, description="Reporter only.")
    body: str | None = Field(None, max_length=MAX_BODY, description="Reporter only.")
    status: str | None = Field(
        None, description="Admin only: open | acknowledged | resolved | deleted (deleted = tombstone)."
    )
    note: str | None = Field(
        None, max_length=MAX_BODY, description="Admin only: the feedback the reporter reads."
    )
