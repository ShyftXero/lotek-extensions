"""JSON API blueprint (mounted at ``<url_prefix>/api``).

Cookie-authed browser surface. When mounted in a host that enforces CSRF (lotek), these routes stay
CSRF-protected (the editor sends ``X-CSRFToken``); the host's role gate blocks viewers on mutating
methods. We additionally check ``host_can_write`` (defense in depth + the standalone case).
"""

from __future__ import annotations

import uuid

from flask import Blueprint, abort, jsonify, request
from sqlalchemy import select

from cream._version import __version__
from cream.deps import (
    current_actor_id,
    current_actor_username,
    get_config,
    host_can_operate_on,
    host_can_write,
    host_visible_engagement_ids,
)
from cream.enums import DocKind
from cream.models import Document
from cream.service import DocumentFrozen, add_line_item, issue, suggest_line_items, totals, void

api_bp = Blueprint("cream_api", __name__)


def _require_write():
    if not host_can_write():
        abort(403)


def _require_operator(engagement_id: uuid.UUID):
    """The engagement-level gate (INV-TENANCY-05): may the current principal operate on this engagement?
    Fails closed (403). The right the extension trusts is the HOST's answer, never the request body."""
    if not host_can_operate_on(engagement_id):
        abort(403)


def _visible_or_none():
    """Scoped engagement id set for list queries, or None (standalone -> no scoping)."""
    return host_visible_engagement_ids()


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _doc_json(doc: Document) -> dict:
    return {
        "id": str(doc.id),
        "kind": doc.kind.value,
        "status": doc.status.value,
        "number": doc.number,
        "title": doc.title,
        "currency": doc.currency,
        "engagement_id": str(doc.engagement_id) if doc.engagement_id else None,
        "line_items": [
            {"id": str(li.id), "description": li.description, "qty": float(li.qty),
             "unit_price": float(li.unit_price), "amount": li.amount, "source": li.source}
            for li in doc.line_items
        ],
        "totals": totals(doc),
    }


@api_bp.get("/health")
def health():
    cfg = get_config()
    with cfg.session_factory() as db:
        has = db.scalar(select(Document.id)) is not None
    return jsonify(status="ok", version=__version__, has_documents=bool(has))


@api_bp.get("/documents")
def list_documents():
    cfg = get_config()
    with cfg.session_factory() as db:
        rows = db.scalars(select(Document).order_by(Document.created_at.desc())).all()
        vis = _visible_or_none()
        if vis is not None:
            rows = [d for d in rows if d.engagement_id in vis]  # read-scope to the actor's engagements
        return jsonify(documents=[_doc_json(d) for d in rows])


@api_bp.post("/documents")
def create_document():
    _require_write()
    body = _body()
    kind = DocKind.quote if body.get("kind") == "quote" else DocKind.invoice
    raw = body.get("engagement_id")
    if not raw:
        abort(400, "engagement_id is required (a document bills exactly one engagement)")
    try:
        eid = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        abort(400, "engagement_id must be a UUID")
    _require_operator(eid)  # engagement-level authz BEFORE any write (INV-TENANCY-05)
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = Document(
            kind=kind,
            title=(body.get("title") or "Untitled")[:255],
            engagement_id=eid,
            owner_id=current_actor_id(),
            created_by=current_actor_username(),
        )
        db.add(doc)
        db.commit()
        return jsonify(_doc_json(doc)), 201


@api_bp.get("/documents/<uuid:doc_id>")
def get_document(doc_id: uuid.UUID):
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            abort(404)
        vis = _visible_or_none()
        if vis is not None and doc.engagement_id not in vis:
            abort(404)  # not in the actor's engagements — 404, don't disclose existence
        return jsonify(_doc_json(doc))


@api_bp.post("/documents/<uuid:doc_id>/line-items")
def add_item(doc_id: uuid.UUID):
    _require_write()
    body = _body()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            abort(404)
        _require_operator(doc.engagement_id)
        try:
            li = add_line_item(
                db, doc,
                description=(body.get("description") or "Item")[:512],
                qty=float(body.get("qty", 1) or 1),
                unit_price=float(body.get("unit_price", 0) or 0),
                source=str(body.get("source") or "manual")[:128],
            )
        except DocumentFrozen as e:
            abort(409, str(e))
        db.commit()
        return jsonify(id=str(li.id)), 201


@api_bp.post("/documents/<uuid:doc_id>/sync")
def sync_document(doc_id: uuid.UUID):
    """Return SUGGESTED line items for engagement units not yet billed. The host route passes the
    engagement's present ``unit_keys``; standalone passes ``[]``. Suggestions only — a human accepts."""
    _require_write()
    body = _body()
    present = [str(k) for k in (body.get("unit_keys") or [])]
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            abort(404)
        _require_operator(doc.engagement_id)
        try:
            sugg = suggest_line_items(db, doc, present)
        except DocumentFrozen as e:
            abort(409, str(e))
        return jsonify(suggestions=[{"unit_key": s.unit_key, "label": s.label,
                                     "unit_price": s.unit_price} for s in sugg])


@api_bp.post("/documents/<uuid:doc_id>/issue")
def issue_document(doc_id: uuid.UUID):
    """Freeze a draft into an immutable, numbered document. Human-only (never agent-autonomous)."""
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            abort(404)
        _require_operator(doc.engagement_id)
        try:
            issue(db, doc)
        except DocumentFrozen as e:
            abort(409, str(e))
        db.commit()
        return jsonify(_doc_json(doc))


@api_bp.post("/documents/<uuid:doc_id>/void")
def void_document(doc_id: uuid.UUID):
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            abort(404)
        _require_operator(doc.engagement_id)
        void(db, doc)
        db.commit()
        return jsonify(_doc_json(doc))
