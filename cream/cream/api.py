"""JSON API blueprint (mounted at ``<url_prefix>/api``).

Cookie-authed browser surface. When mounted in a host that enforces CSRF (lotek), these routes stay
CSRF-protected (the editor sends ``X-CSRFToken``); the host's role gate blocks viewers on mutating
methods. We additionally check ``host_can_write`` (defense in depth + the standalone case).

Two conventions worth knowing before adding a route:

* **Every mutating route resolves the document first, then asks the host** whether the caller may
  operate on *that document's* engagement — never on an engagement id from the request body, which the
  caller controls.
* **Money crosses this boundary as JSON floats and comes back as ``Decimal``** (:mod:`cream.money`).
  Nothing here does arithmetic; ``service`` owns that, so the API and the renderer cannot disagree about
  a total.
"""

from __future__ import annotations

import json
import re
import uuid

from flask import Blueprint, abort, jsonify, request
from sqlalchemy import select

from cream._version import __version__
from cream.deps import (
    current_actor_id,
    current_actor_is_admin,
    current_actor_username,
    get_config,
    host_can_operate_on,
    host_can_write,
    host_engagement_burn,
    host_engagement_scope,
    host_engagement_units,
    host_visible_engagement_ids,
)
from cream.enums import COMMON_UNITS, DEFAULT_UNIT, DocKind, DocStatus
from cream.models import Document
from cream.money import as_json, money, pct
from cream.render import render_document_html
from cream.service import (
    DocumentFrozen,
    NotConvertible,
    accept,
    add_line_item,
    burn_rows,
    convert_to_invoice,
    get_brand,
    issue,
    mark_sent,
    replace_line_items,
    set_scope,
    suggest_line_items,
    totals,
    update_document,
    void,
)
from cream.viewmodel import build_view, view_for

api_bp = Blueprint("cream_api", __name__)

#: Raster image data URIs only. SVG is excluded on purpose — it is a document format with its own
#: fetching and scripting surface, and a logo has no need of one.
_LOGO_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=\s]+$")
_MAX_LOGO_CHARS = 2_000_000  # ~1.5 MB decoded

_BRAND_TEXT = {
    "company_name": 255,
    "email": 255,
    "phone": 64,
    "website": 255,
    "tax_id": 64,
    "accent_color": 32,
    "font_stack": 255,
    "default_currency": 8,
    "default_tax_label": 64,
}
_BRAND_LONGTEXT = ("address", "payment_instructions", "footer_terms", "default_roe_terms")


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


def _load(db, doc_id: uuid.UUID, *, write: bool) -> Document:
    """Fetch a document and apply the read/write gate.

    A document outside the caller's visible engagements 404s rather than 403s — a 403 would confirm that
    a document with this id exists, which is exactly what an id-guessing probe is looking for.
    """
    doc = db.get(Document, doc_id)
    if doc is None:
        abort(404)
    vis = _visible_or_none()
    if vis is not None and doc.engagement_id not in vis:
        abort(404)
    if write:
        _require_operator(doc.engagement_id)
    return doc


def _clean_logo(value):
    """A vetted ``data:image/...;base64,`` URI, or ``None``. Anything else is dropped, never stored.

    The renderer re-checks this (``render.safe_logo``) — belt and braces, because the consequence of a
    remote URL reaching the PDF engine is a server-side fetch to an attacker-chosen host.
    """
    if value in (None, ""):
        return None
    candidate = str(value).strip()
    if len(candidate) > _MAX_LOGO_CHARS or not _LOGO_RE.match(candidate):
        return None
    return "".join(candidate.split())


def _line_json(li) -> dict:
    return {
        "id": str(li.id),
        "description": li.description,
        "detail": li.detail,
        "qty": as_json(money(li.qty)),
        "unit": li.unit,
        "unit_price": as_json(money(li.unit_price)),
        "amount": as_json(li.amount),
        "source": li.source,
    }


def _totals_json(tot) -> dict:
    return {
        "subtotal": as_json(tot.subtotal),
        "discount": as_json(tot.discount),
        "discount_label": tot.discount_label,
        "taxable": as_json(tot.taxable),
        "tax": as_json(tot.tax),
        "tax_label": tot.tax_label,
        "total": as_json(tot.total),
    }


def _doc_json(doc: Document) -> dict:
    scope: list[str] = []
    if doc.scope_json:
        try:
            parsed = json.loads(doc.scope_json)
            scope = [str(s) for s in parsed] if isinstance(parsed, list) else []
        except ValueError:
            scope = []
    return {
        "id": str(doc.id),
        "kind": doc.kind.value,
        "status": doc.status.value,
        "editable": doc.status is DocStatus.draft,
        "number": doc.number,
        "title": doc.title,
        "currency": doc.currency,
        "reference": doc.reference,
        "engagement_id": str(doc.engagement_id) if doc.engagement_id else None,
        "client_id": str(doc.client_id) if doc.client_id else None,
        "converted_from_id": str(doc.converted_from_id) if doc.converted_from_id else None,
        "bill_to_name": doc.bill_to_name,
        "bill_to_attn": doc.bill_to_attn,
        "bill_to_address": doc.bill_to_address,
        "bill_to_email": doc.bill_to_email,
        "issued_at": doc.issued_at.isoformat() if doc.issued_at else None,
        "sent_at": doc.sent_at.isoformat() if doc.sent_at else None,
        "accepted_at": doc.accepted_at.isoformat() if doc.accepted_at else None,
        "valid_until": doc.valid_until.isoformat() if doc.valid_until else None,
        "due_date": doc.due_date.isoformat() if doc.due_date else None,
        "window_start": doc.window_start.isoformat() if doc.window_start else None,
        "window_end": doc.window_end.isoformat() if doc.window_end else None,
        "discount_pct": as_json(pct(doc.discount_pct)),
        "discount_amount": as_json(None if doc.discount_amount is None else money(doc.discount_amount)),
        "discount_label": doc.discount_label,
        "tax_label": doc.tax_label,
        "tax_pct": as_json(pct(doc.tax_pct)),
        "notes": doc.notes,
        "scope": scope,
        "authorization_required": bool(doc.authorization_required),
        "signatory_name": doc.signatory_name,
        "signatory_title": doc.signatory_title,
        "roe_terms": doc.roe_terms,
        "line_items": [_line_json(li) for li in doc.line_items],
        "totals": _totals_json(totals(doc)),
    }


def _brand_json(brand) -> dict:
    return {
        "company_name": brand.company_name,
        "address": brand.address,
        "email": brand.email,
        "phone": brand.phone,
        "website": brand.website,
        "tax_id": brand.tax_id,
        "logo_data_uri": brand.logo_data_uri,
        "accent_color": brand.accent_color,
        "font_stack": brand.font_stack,
        "default_currency": brand.default_currency,
        "default_tax_label": brand.default_tax_label,
        "default_tax_pct": as_json(pct(brand.default_tax_pct)),
        "payment_instructions": brand.payment_instructions,
        "footer_terms": brand.footer_terms,
        "default_roe_terms": brand.default_roe_terms,
    }


# --- meta ----------------------------------------------------------------------------------------


@api_bp.get("/health")
def health():
    cfg = get_config()
    with cfg.session_factory() as db:
        has = db.scalar(select(Document.id)) is not None
    return jsonify(status="ok", version=__version__, has_documents=bool(has), units=list(COMMON_UNITS))


# --- brand ---------------------------------------------------------------------------------------


@api_bp.get("/brand")
def read_brand():
    cfg = get_config()
    with cfg.session_factory() as db:
        brand = get_brand(db)
        db.commit()
        return jsonify(_brand_json(brand))


@api_bp.put("/brand")
def write_brand():
    """Update the issuer identity. **Admin only.**

    Branding is global — there is no engagement to scope it to — and it carries
    ``payment_instructions``: the block telling a client where to send money. Gating that on ordinary
    write capability would let anyone who can edit a line item silently re-route remittance on every
    future invoice, which is invoice fraud with no per-document trace. Standalone (no host actor hook)
    is a single local user and stays permitted; mounted, it needs an admin.
    """
    _require_write()
    if not current_actor_is_admin():
        abort(403, "branding is admin-only (it sets the remit-to details on every document)")
    body = _body()
    cfg = get_config()
    with cfg.session_factory() as db:
        brand = get_brand(db)
        for name, limit in _BRAND_TEXT.items():
            if name in body:
                raw = body.get(name)
                setattr(brand, name, (str(raw).strip()[:limit] if raw not in (None, "") else None))
        for name in _BRAND_LONGTEXT:
            if name in body:
                raw = body.get(name)
                setattr(brand, name, (str(raw) if raw not in (None, "") else None))
        if "default_tax_pct" in body:
            brand.default_tax_pct = pct(body.get("default_tax_pct"))
        if "logo_data_uri" in body:
            brand.logo_data_uri = _clean_logo(body.get("logo_data_uri"))
        # These carry renderer defaults rather than being nullable in practice.
        brand.company_name = brand.company_name or "Your Firm"
        brand.default_currency = brand.default_currency or "USD"
        brand.accent_color = brand.accent_color or "#0f766e"
        db.commit()
        return jsonify(_brand_json(brand))


# --- documents -----------------------------------------------------------------------------------


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
        brand = get_brand(db)
        doc = Document(
            kind=kind,
            title=(body.get("title") or "Untitled")[:255],
            engagement_id=eid,
            currency=(body.get("currency") or brand.default_currency or "USD")[:8],
            tax_label=body.get("tax_label") or brand.default_tax_label,
            tax_pct=pct(body.get("tax_pct"), default=pct(brand.default_tax_pct)),
            roe_terms=body.get("roe_terms") or (brand.default_roe_terms if kind is DocKind.quote else None),
            authorization_required=bool(body.get("authorization_required")) or kind is DocKind.quote,
            owner_id=current_actor_id(),
            created_by=current_actor_username(),
        )
        client_raw = body.get("client_id")
        if client_raw:
            try:
                doc.client_id = uuid.UUID(str(client_raw))
            except (ValueError, TypeError):
                abort(400, "client_id must be a UUID")
        db.add(doc)
        db.flush()
        update_document(db, doc, body)
        db.commit()
        return jsonify(_doc_json(doc)), 201


@api_bp.get("/documents/<uuid:doc_id>")
def get_document(doc_id: uuid.UUID):
    cfg = get_config()
    with cfg.session_factory() as db:
        return jsonify(_doc_json(_load(db, doc_id, write=False)))


@api_bp.put("/documents/<uuid:doc_id>")
def save_document(doc_id: uuid.UUID):
    """Save the whole draft — fields, and the line items when the body carries them.

    A full replace rather than a patch protocol: the editor owns the entire document, and two ways to
    express "these are the lines now" is one more than can be kept consistent.
    """
    _require_write()
    body = _body()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
        try:
            update_document(db, doc, body)
            if isinstance(body.get("line_items"), list):
                replace_line_items(db, doc, body["line_items"])
        except DocumentFrozen as exc:
            abort(409, str(exc))
        db.commit()
        return jsonify(_doc_json(doc))


@api_bp.post("/documents/<uuid:doc_id>/line-items")
def add_item(doc_id: uuid.UUID):
    _require_write()
    body = _body()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
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
        except DocumentFrozen as e:
            abort(409, str(e))
        db.commit()
        return jsonify(_line_json(li)), 201


@api_bp.post("/documents/<uuid:doc_id>/preview")
def preview_document(doc_id: uuid.UUID):
    """Render UNSAVED editor state through the real renderer and return the HTML fragment.

    The unsaved values are applied inside a **savepoint that is always rolled back**, so the preview is
    produced by literally the same ``update_document`` / ``replace_line_items`` / ``build_view`` path a
    save would take. A second renderer in JavaScript would be faster and would eventually disagree with
    the PDF; a preview you cannot trust is worse than none.
    """
    _require_write()
    body = _body()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
        brand = get_brand(db)
        if doc.status is not DocStatus.draft:
            return jsonify(html=render_document_html(view_for(doc, brand)), frozen=True)
        savepoint = db.begin_nested()
        try:
            update_document(db, doc, body)
            if isinstance(body.get("line_items"), list):
                replace_line_items(db, doc, body["line_items"])
            view = build_view(doc, brand)
            payload = {"html": render_document_html(view), "totals": _totals_json(view.totals)}
        finally:
            savepoint.rollback()  # a preview must never persist, on any path out of this block
        return jsonify(**payload, frozen=False)


@api_bp.post("/documents/<uuid:doc_id>/sync")
def sync_document(doc_id: uuid.UUID):
    """Return SUGGESTED line items for engagement units not yet billed.

    ``unit_keys`` may be supplied by a host route that already knows the engagement's shape; when the
    body omits the key entirely (the browser case) they are read from the host seam instead. Either way
    these are suggestions — nothing is priced onto the document without a human accepting it.
    """
    _require_write()
    body = _body()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
        if "unit_keys" in body:
            present = [str(k) for k in (body.get("unit_keys") or [])]
        else:
            present = host_engagement_units(doc.engagement_id)
        try:
            sugg = suggest_line_items(db, doc, present)
        except DocumentFrozen as e:
            abort(409, str(e))
        return jsonify(suggestions=[{"unit_key": s.unit_key, "label": s.label,
                                     "unit_price": as_json(s.unit_price), "unit": s.unit}
                                    for s in sugg])


@api_bp.post("/documents/<uuid:doc_id>/scope-sync")
def scope_sync(doc_id: uuid.UUID):
    """Pull the engagement's real targets from the host and freeze them onto the draft as Appendix A.

    The host is the only authority here: a scope list supplied in the request body would let a caller
    print an authorization appendix for ranges the engagement never covered.
    """
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
        targets = host_engagement_scope(doc.engagement_id)
        try:
            set_scope(db, doc, targets)
        except DocumentFrozen as e:
            abort(409, str(e))
        db.commit()
        return jsonify(scope=targets, count=len(targets))


@api_bp.get("/documents/<uuid:doc_id>/burn")
def document_burn(doc_id: uuid.UUID):
    """Quoted vs actually-executed quantity per line. Advisory; never rendered on a client document."""
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=False)
        measured = host_engagement_burn(doc.engagement_id)
        rows = burn_rows(doc, measured)
        return jsonify(
            available=bool(measured),
            rows=[
                {
                    "description": r.description,
                    "source": r.source,
                    "unit": r.unit,
                    "quoted_qty": as_json(r.quoted_qty),
                    "executed_qty": as_json(r.executed_qty),
                    "delta": as_json(r.delta),
                }
                for r in rows
            ],
        )


# --- lifecycle -----------------------------------------------------------------------------------


@api_bp.post("/documents/<uuid:doc_id>/issue")
def issue_document(doc_id: uuid.UUID):
    """Freeze a draft into an immutable, numbered document. Human-only (never agent-autonomous)."""
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
        try:
            issue(db, doc, brand=get_brand(db))
        except DocumentFrozen as e:
            abort(409, str(e))
        db.commit()
        return jsonify(_doc_json(doc))


@api_bp.post("/documents/<uuid:doc_id>/mark-sent")
def mark_sent_document(doc_id: uuid.UUID):
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
        try:
            mark_sent(db, doc)
        except DocumentFrozen as e:
            abort(409, str(e))
        db.commit()
        return jsonify(_doc_json(doc))


@api_bp.post("/documents/<uuid:doc_id>/accept")
def accept_document(doc_id: uuid.UUID):
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
        try:
            accept(db, doc)
        except (DocumentFrozen, NotConvertible) as e:
            abort(409, str(e))
        db.commit()
        return jsonify(_doc_json(doc))


@api_bp.post("/documents/<uuid:doc_id>/convert")
def convert_document(doc_id: uuid.UUID):
    """Turn an issued/accepted quote into a NEW invoice draft. The quote stays frozen."""
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        quote = _load(db, doc_id, write=True)
        try:
            invoice = convert_to_invoice(db, quote, owner_id=current_actor_id(),
                                         created_by=current_actor_username())
        except NotConvertible as e:
            abort(409, str(e))
        db.commit()
        return jsonify(_doc_json(invoice)), 201


@api_bp.post("/documents/<uuid:doc_id>/void")
def void_document(doc_id: uuid.UUID):
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id, write=True)
        void(db, doc)
        db.commit()
        return jsonify(_doc_json(doc))
