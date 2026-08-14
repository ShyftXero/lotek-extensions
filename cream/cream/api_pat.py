"""PAT-scoped MACHINE API for Cream — mounted at ``<url_prefix>/machine`` on its OWN blueprint.

Lets host TOOLS (an agent on a personal access token) DRAFT invoices/quotes the host's way (Bearer +
scope RBAC), the same contract lotek's ``/api/v1`` and scribble's machine API use. Distinct from the
cookie-authed browser API at ``<url_prefix>/api``.

FINALIZATION IS HUMAN-ONLY — the load-bearing security line:
  * this surface exposes DRAFTING only: list/get/create documents, add line items, and sync (suggestions).
  * there are deliberately NO ``/issue`` or ``/void`` routes. Freezing a draft into an immutable numbered
    document, and voiding an issued one, are financial state changes cream marks "human-only (never
    agent-autonomous)". A PAT drafts; a human issues/voids in the dashboard.

TENANCY: a machine request has no session, so ``cream.deps.current_actor_*`` are None here. Identity comes
from the PAT principal (``host.actor()``); the engagement-operator gate (``host_can_operate_on``) and the
visible-engagement scoping (``host_visible_engagement_ids``) are principal-based hooks, correct for a PAT.
Every write is authorized on the engagement BEFORE it runs (INV-TENANCY-05), never on the request body.
SECURITY otherwise as scribble/vector (before_request auth, per-route scope, CSRF/session exempt only
because it takes no cookie).

SHAPE: the document/line JSON is produced by ``cream.api``'s serializers, not a second copy of them. An
agent reading a document over this surface and over the browser one must not be told two different
shapes, and money must cross both boundaries the same way (:mod:`cream.money`).

IDEMPOTENCY: the create-shaped writes (``POST /documents``, ``POST /documents/<id>/line-items``) run
through the host's injected idempotency seam (``extras['idempotent']``, the same one core reuses for its
resource-minting POSTs). An agent that retries a create with the same ``Idempotency-Key`` header (or
``idempotency_key`` body field) gets the ORIGINAL resource back instead of a duplicate; with no key the
seam calls the handler directly and behavior is byte-for-byte unchanged. ``/sync`` writes nothing and
``/issue``/``/void`` deliberately do not exist (human-only), so neither is wrapped.
"""

from __future__ import annotations

import uuid

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from cream import host

# Deliberately the browser API's own serializers (single source of truth for the wire shape) rather than
# a machine-local copy that would silently drift from it as cream's document model grows.
from cream.api import _doc_json, _line_json
from cream.api_schemas import AddLineItemRequest, CreateDocumentRequest, SyncRequest, request_body
from cream.deps import get_config, host_can_operate_on, host_visible_engagement_ids
from cream.enums import DEFAULT_UNIT, DocKind
from cream.models import Document
from cream.money import as_json, pct
from cream.service import DocumentFrozen, add_line_item, get_brand, suggest_line_items

machine_bp = Blueprint("cream_machine", __name__)
machine_bp.before_request(host.authenticate)


# The bodies live as plain dicts so the idempotency seam (below) can also return them: ``produce`` must
# hand the seam ``(dict, int)`` — a jsonified ``Response`` would be un-storable — while the cookieless
# non-wrapped routes still return them jsonified.
_FORBIDDEN_BODY = {"error": "forbidden", "detail": "not an operator on this engagement"}
_NOT_FOUND_BODY = {"error": "not_found", "detail": "document not found"}


def _forbidden():
    return jsonify(_FORBIDDEN_BODY), 403


def _not_found():
    return jsonify(_NOT_FOUND_BODY), 404


def _idempotency_key() -> str | None:
    """The request's idempotency key, or ``None``. The ``Idempotency-Key`` header wins; a body
    ``idempotency_key`` field is the fallback so a client that cannot set headers can still opt in. An
    empty/whitespace value is treated as absent (the seam then runs the mutation directly). Mirrors
    core's ``app.api_v1._idempotency_key``."""
    header = (request.headers.get("Idempotency-Key") or "").strip()
    if header:
        return header
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        field = body.get("idempotency_key")
        if isinstance(field, str) and field.strip():
            return field.strip()
    return None


def _idempotent(actor, key, produce):
    """Run ``produce`` (``() -> (dict, int)``) through the HOST idempotency seam so a retried create with
    the same key returns the ORIGINAL resource instead of a duplicate — the same seam core reuses for its
    own resource-minting POSTs (``extras['idempotent']``). The seam itself is a no-op when ``key`` is
    falsy (it calls ``produce`` directly and stores nothing), so behavior is byte-for-byte unchanged
    without a key. Falls back to a direct call if the host injected no seam (older host), never failing
    closed — idempotency is an enhancement, not an auth gate."""
    seam = host.host_hook("idempotent")
    if seam is None:
        return produce()
    return seam(actor, key, produce)


def _load_visible_or_none(db, doc_id: uuid.UUID) -> Document | None:
    """The document, if it is inside the token's visible engagements. Missing and not-visible return the
    SAME ``None`` (the caller 404s both) — a 403 for the second would confirm the id exists, which is
    exactly what an id-guessing probe is after. Mirrors ``cream.api._load``'s read gate."""
    doc = db.get(Document, doc_id)
    if doc is None:
        return None
    vis = host_visible_engagement_ids()
    if vis is not None and doc.engagement_id not in vis:
        return None
    return doc


@machine_bp.get("/documents")
@host.require_scope("read")
def list_documents():
    """List documents for the token's user's engagements (read-scoped to those engagements)."""
    with get_config().session_factory() as db:
        rows = db.scalars(select(Document).order_by(Document.created_at.desc())).all()
        vis = host_visible_engagement_ids()
        if vis is not None:
            rows = [d for d in rows if d.engagement_id in vis]
        return jsonify(documents=[_doc_json(d) for d in rows])


@machine_bp.get("/documents/<uuid:doc_id>")
@host.require_scope("read")
def get_document(doc_id: uuid.UUID):
    """Fetch one document, if it belongs to one of the token's user's engagements."""
    with get_config().session_factory() as db:
        doc = _load_visible_or_none(db, doc_id)
        if doc is None:
            return _not_found()
        return jsonify(_doc_json(doc))


@machine_bp.post("/documents")
@host.require_scope("write")
@request_body(CreateDocumentRequest)
def create_document():
    """Create a DRAFT invoice/quote billing one engagement (the token's user must operate on it).

    The brand defaults (currency, tax label/rate, quote RoE terms) are applied exactly as the browser
    ``POST <url_prefix>/api/documents`` applies them — an agent-created draft that rendered with a
    different currency or no tax line than a dashboard-created one would be a silent reporting defect.

    Retried-create protection: an ``Idempotency-Key`` header (or ``idempotency_key`` body field) makes a
    replayed POST return the ORIGINAL created document instead of minting a second draft — the whole
    handler is ``produce``, so with no key supplied the seam calls it directly and behavior is unchanged.
    """
    actor = host.actor()
    key = _idempotency_key()

    def produce() -> tuple[dict, int]:
        body = request.get_json(silent=True) or {}
        kind = DocKind.quote if body.get("kind") == "quote" else DocKind.invoice
        raw = body.get("engagement_id")
        if not raw:
            return {"error": "bad_request", "detail": "engagement_id is required"}, 400
        try:
            eid = uuid.UUID(str(raw))
        except (ValueError, TypeError):
            return {"error": "bad_request", "detail": "engagement_id must be a UUID"}, 400
        if not host_can_operate_on(eid):  # engagement-level authz BEFORE any write (INV-TENANCY-05)
            return _FORBIDDEN_BODY, 403
        client_id = None
        client_raw = body.get("client_id")
        if client_raw:
            try:
                client_id = uuid.UUID(str(client_raw))
            except (ValueError, TypeError):
                return {"error": "bad_request", "detail": "client_id must be a UUID"}, 400
        with get_config().session_factory() as db:
            brand = get_brand(db)
            doc = Document(
                kind=kind,
                title=(body.get("title") or "Untitled")[:255],
                engagement_id=eid,
                client_id=client_id,
                currency=(body.get("currency") or brand.default_currency or "USD")[:8],
                tax_label=body.get("tax_label") or brand.default_tax_label,
                tax_pct=pct(body.get("tax_pct"), default=pct(brand.default_tax_pct)),
                roe_terms=(
                    body.get("roe_terms")
                    or (brand.default_roe_terms if kind is DocKind.quote else None)
                ),
                authorization_required=bool(body.get("authorization_required")) or kind is DocKind.quote,
                # Attribution comes from the PAT principal. `current_actor_id()` is session-only and is None
                # on a machine request, which would leave every agent-drafted document unattributed.
                owner_id=getattr(actor, "id", None),
                created_by=getattr(actor, "username", None),
            )
            db.add(doc)
            db.commit()
            return _doc_json(doc), 201

    body_out, status = _idempotent(actor, key, produce)
    return jsonify(body_out), status


@machine_bp.post("/documents/<uuid:doc_id>/line-items")
@host.require_scope("write")
@request_body(AddLineItemRequest)
def add_item(doc_id: uuid.UUID):
    """Add a line item to a draft document (operator on its engagement only).

    Wrapped in the same retried-create idempotency seam as ``create_document``: a replayed POST with the
    same ``Idempotency-Key`` returns the ORIGINAL added line item rather than appending a duplicate. No
    key supplied → the seam runs ``produce`` directly and the end state is byte-for-byte unchanged.
    """
    actor = host.actor()
    key = _idempotency_key()

    def produce() -> tuple[dict, int]:
        body = request.get_json(silent=True) or {}
        with get_config().session_factory() as db:
            doc = _load_visible_or_none(db, doc_id)
            if doc is None:
                return _NOT_FOUND_BODY, 404
            # Resolve the document FIRST, then ask the host about *that document's* engagement — never
            # about an engagement id the caller supplied.
            if not host_can_operate_on(doc.engagement_id):
                return _FORBIDDEN_BODY, 403
            try:
                li = add_line_item(
                    db, doc,
                    description=(body.get("description") or "Item")[:512],
                    detail=body.get("detail"),
                    qty=body.get("qty", 1),
                    unit=str(body.get("unit") or DEFAULT_UNIT)[:16],
                    unit_price=body.get("unit_price", 0),
                    source=str(body.get("source") or "manual")[:128],
                )
            except DocumentFrozen as _e:
                return {"error": "conflict", "detail": "Document can no longer be modified."}, 409
            db.commit()
            return _line_json(li), 201

    body_out, status = _idempotent(actor, key, produce)
    return jsonify(body_out), status


@machine_bp.post("/documents/<uuid:doc_id>/sync")
@host.require_scope("write")
@request_body(SyncRequest)
def sync_document(doc_id: uuid.UUID):
    """Return SUGGESTED line items for engagement units not yet billed (suggestions only; nothing is
    written — a human accepts them)."""
    body = request.get_json(silent=True) or {}
    present = [str(k) for k in (body.get("unit_keys") or [])]
    with get_config().session_factory() as db:
        doc = _load_visible_or_none(db, doc_id)
        if doc is None:
            return _not_found()
        if not host_can_operate_on(doc.engagement_id):
            return _forbidden()
        try:
            sugg = suggest_line_items(db, doc, present)
        except DocumentFrozen as _e:
            return jsonify({"error": "conflict", "detail": "Document can no longer be modified."}), 409
        return jsonify(suggestions=[
            {"unit_key": s.unit_key, "label": s.label, "unit_price": as_json(s.unit_price), "unit": s.unit}
            for s in sugg
        ])
