"""Declarative request schemas for Cream's PAT machine API — schema source for the host's OpenAPI doc.

``request_body`` stamps the same conventional attribute the host generator reads
(``app.api_schemas.REQUEST_MODEL_ATTR``), spelled as a literal (an extension must not import a host).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

REQUEST_MODEL_ATTR = "__lotek_request_model__"


def request_body(model: type[BaseModel]):
    def deco(fn):
        setattr(fn, REQUEST_MODEL_ATTR, model)
        return fn

    return deco


class CreateDocumentRequest(BaseModel):
    """Body of ``POST /cream/machine/documents`` — create a DRAFT invoice/quote (write scope).

    Finalization (issue/void) is human-only and is not part of the machine API. Every field below except
    ``engagement_id`` is optional; omitted ones fall back to the issuer's brand defaults, exactly as the
    browser create route does.
    """

    kind: str | None = Field("invoice", description="'invoice' | 'quote' (default 'invoice').")
    title: str | None = Field(None, description="Document title (default 'Untitled').")
    engagement_id: str = Field(
        ...,
        description="UUID of the engagement this document bills. The token's user must hold an operator "
        "membership on it.",
    )
    client_id: str | None = Field(None, description="UUID of the host Client to attribute the document to.")
    currency: str | None = Field(None, description="ISO currency code (default: the brand's).")
    tax_label: str | None = Field(None, description="Free-text tax label, e.g. 'VAT 20%' (default: brand).")
    tax_pct: float | None = Field(None, description="Tax rate percentage (default: the brand's).")
    roe_terms: str | None = Field(None, description="Rules-of-engagement terms (quotes default to brand).")
    authorization_required: bool | None = Field(
        None, description="Require a signatory block. Always true for a quote."
    )


class AddLineItemRequest(BaseModel):
    """Body of ``POST /cream/machine/documents/{doc_id}/line-items`` (write scope)."""

    description: str | None = Field(None, description="Line-item description (default 'Item').")
    detail: str | None = Field(None, description="Scoping prose under the description (restricted markup).")
    qty: float | None = Field(1, description="Quantity (default 1).")
    unit: str | None = Field(None, description="Unit of measure, e.g. 'day' (default: cream's default).")
    unit_price: float | None = Field(0, description="Unit price (default 0).")
    source: str | None = Field("manual", description="Provenance tag (default 'manual').")


class SyncRequest(BaseModel):
    """Body of ``POST /cream/machine/documents/{doc_id}/sync`` — returns SUGGESTED line items only."""

    unit_keys: list[str] | None = Field(None, description="Engagement unit keys already billed elsewhere.")
