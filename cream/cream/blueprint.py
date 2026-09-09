"""Human-facing UI blueprint — the document list, the editor, and the export surfaces.

Route names are a stable contract used by templates and the host nav: ``cream.dashboard``,
``cream.new_document``, ``cream.edit_document``, ``cream.view_document``, ``cream.export_html``,
``cream.export_pdf``, ``cream.brand_settings``.

Read gating mirrors the API: a document outside the actor's visible engagements 404s rather than 403s,
so the UI cannot be used to probe for the existence of another tenant's documents.
"""

from __future__ import annotations

import json
import uuid

from flask import Blueprint, Response, abort, render_template, request
from sqlalchemy import select

from cream._version import __version__
from cream.deps import (
    current_actor_is_admin,
    get_config,
    host_can_write,
    host_visible_engagement_ids,
)
from cream.enums import COMMON_UNITS, DocKind, DocStatus
from cream.handles import document_handle, export_stem
from cream.models import Document
from cream.money import as_json, money, pct
from cream.render import render_document_html, render_document_pdf
from cream.service import get_brand, totals
from cream.viewmodel import view_for

bp = Blueprint("cream", __name__, template_folder="templates")


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "cream_base": cfg.base_template,
        "cream_version": __version__,
        "cream_can_write": host_can_write(),
        # Branding sets the remit-to block on every document, so its form is admin-only — matching the
        # gate on `PUT /api/brand`. The template hiding the button is a courtesy; the API is the control.
        "cream_is_admin": current_actor_is_admin(),
        "cream_units": COMMON_UNITS,
        "api_base": cfg.url_prefix.rstrip("/") + "/api",
    }


def _load(db, doc_id: uuid.UUID) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None:
        abort(404)
    vis = host_visible_engagement_ids()
    if vis is not None and doc.engagement_id not in vis:
        abort(404)  # not in the actor's engagements — 404, don't disclose existence
    return doc


def _view_meta(doc: Document, *, editable: bool) -> dict:
    """The header facts ``view.html`` shows above the rendered document.

    ``number`` stays the raw column (NULL until issue). ``handle`` is what the heading NAMES the document
    by — the number once it has one, and a tail-truncated-id handle before that, so an unissued document
    is not headed just ``Invoice (draft)`` with nothing to tell it from the other four drafts (ext#46).
    """
    return {"kind": doc.kind.value, "status": doc.status.value, "number": doc.number,
            "handle": document_handle(doc.number, doc.status.value, doc.id),
            "editable": editable}


def _editor_payload(doc: Document) -> dict:
    """The document as the editor's JavaScript wants it — money as numbers, dates as ``YYYY-MM-DD``
    for ``<input type=date>``, everything else a plain string.

    ``handle`` is the one key here that is not a form field: the editor is where a draft is actually
    worked, and the *only* action a draft's row offers (``list.html``'s per-row link is ``Edit``, not
    ``View``), so its heading and tab title need the same identity the list cell got — two editors open on
    two drafts were both headed ``Invoice draft`` (ext#46 review round 1). ``service.update_document``
    ignores keys it does not know, so this rides along in the state the editor PUTs back without reaching
    anything writable.
    """
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
        "handle": document_handle(doc.number, doc.status.value, doc.id),
        "scope": scope,
        "title": doc.title or "",
        "currency": doc.currency or "USD",
        "reference": doc.reference or "",
        "bill_to_name": doc.bill_to_name or "",
        "bill_to_attn": doc.bill_to_attn or "",
        "bill_to_address": doc.bill_to_address or "",
        "bill_to_email": doc.bill_to_email or "",
        "valid_until": doc.valid_until.isoformat() if doc.valid_until else "",
        "due_date": doc.due_date.isoformat() if doc.due_date else "",
        "window_start": doc.window_start.isoformat() if doc.window_start else "",
        "window_end": doc.window_end.isoformat() if doc.window_end else "",
        "discount_pct": as_json(pct(doc.discount_pct)),
        "discount_amount": as_json(None if doc.discount_amount is None else money(doc.discount_amount)),
        "discount_label": doc.discount_label or "",
        "tax_label": doc.tax_label or "",
        "tax_pct": as_json(pct(doc.tax_pct)),
        "notes": doc.notes or "",
        "authorization_required": bool(doc.authorization_required),
        "signatory_name": doc.signatory_name or "",
        "signatory_title": doc.signatory_title or "",
        "roe_terms": doc.roe_terms or "",
        "line_items": [
            {
                "description": li.description or "",
                "detail": li.detail or "",
                "qty": as_json(money(li.qty)),
                "unit": li.unit or "project",
                "unit_price": as_json(money(li.unit_price)),
                "source": li.source or "manual",
            }
            for li in doc.line_items
        ],
    }


# ponytail: a flat cap, not offset paging. The most-recent page plus the status/kind filters covers a
# firm's day-to-day; add real pagination only if this ceiling is ever actually hit.
_DASHBOARD_MAX = 200


def _as_doc_enum(enum_cls, raw: str):
    """A blank/unknown filter value -> None (no filter), never a 500 — same fail-open posture as the id
    parsers. Enum-validating here is also what keeps the raw query-arg off the page as free text."""
    try:
        return enum_cls(raw)
    except ValueError:
        return None


@bp.get("/")
def dashboard():
    cfg = get_config()
    rows = []
    vis = host_visible_engagement_ids()
    status = _as_doc_enum(DocStatus, (request.args.get("status") or "").strip())
    kind = _as_doc_enum(DocKind, (request.args.get("kind") or "").strip())
    with cfg.session_factory() as db:
        stmt = select(Document).order_by(Document.created_at.desc())
        if vis is not None:
            # read-scope in SQL (was a post-query Python skip) so the row cap below is correct
            stmt = stmt.where(Document.engagement_id.in_(vis))
        if status is not None:
            stmt = stmt.where(Document.status == status)
        if kind is not None:
            stmt = stmt.where(Document.kind == kind)
        fetched = db.scalars(stmt.limit(_DASHBOARD_MAX + 1)).all()
        truncated = len(fetched) > _DASHBOARD_MAX
        for d in fetched[:_DASHBOARD_MAX]:
            rows.append({
                "id": str(d.id), "kind": d.kind.value, "status": d.status.value,
                # An unissued document has no number, and this cell is also the row's LINK — so a bare
                # "—" here was a one-character click target with no identity (ext#46). `document_handle`
                # falls back to a tail-truncated id: `draft …b839c91e20`.
                #
                # Named `handle`, NOT `number`, and deliberately so: `_view_meta` above keeps `number` as
                # the raw NULL-until-issue column and publishes the display string separately, and one
                # file cannot hold both conventions. A key called `number` whose value may be
                # `draft …b839c91e20` is the trap that gets a synthesized identifier into a sort, a
                # filter, or a JSON response someone builds out of these rows later — the machine surface
                # reports `number: null` for a draft on purpose, and this must not quietly disagree.
                "handle": document_handle(d.number, d.status.value, d.id), "title": d.title,
                # Left as an em-dash on purpose: a blank bill-to is a MISSING FIELD, not a missing
                # identifier. Substituting an id tail here would print the document's id under a column
                # headed "Bill to", inventing an identity for a client record that may not exist yet.
                "client": d.bill_to_name or "—",
                "editable": d.status is DocStatus.draft,
                "total": totals(d).total, "currency": d.currency,
            })
    return render_template(
        "cream/list.html", documents=rows, truncated=truncated, cream_list_limit=_DASHBOARD_MAX,
        filter_status=(status.value if status is not None else ""),
        filter_kind=(kind.value if kind is not None else ""),
        statuses=[s.value for s in DocStatus], kinds=[k.value for k in DocKind],
    )


@bp.get("/documents/new")
def new_document():
    """The create form. A document must name the engagement it bills, so the form asks for it up front
    rather than letting a document exist unattached."""
    return render_template("cream/new.html")


@bp.get("/documents/<uuid:doc_id>")
def view_document(doc_id: uuid.UUID):
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id)
        brand = get_brand(db)
        db.commit()  # get_brand may have created the singleton on first ever view
        html = render_document_html(view_for(doc, brand))
        meta = _view_meta(doc, editable=doc.status is DocStatus.draft)
    return render_template("cream/view.html", doc_html=html, doc_id=str(doc_id), meta=meta)


@bp.get("/documents/<uuid:doc_id>/edit")
def edit_document(doc_id: uuid.UUID):
    """Form on the left, live server-rendered preview on the right."""
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id)
        brand = get_brand(db)
        db.commit()
        if doc.status is not DocStatus.draft:
            # Nothing to edit — send the reader to the frozen view rather than showing dead inputs.
            html = render_document_html(view_for(doc, brand))
            meta = _view_meta(doc, editable=False)
            return render_template("cream/view.html", doc_html=html, doc_id=str(doc_id), meta=meta)
        payload = _editor_payload(doc)
        initial = render_document_html(view_for(doc, brand))
    return render_template("cream/edit.html", doc=payload, doc_id=str(doc_id), initial_html=initial)


@bp.get("/documents/<uuid:doc_id>/export.html")
def export_html(doc_id: uuid.UUID):
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id)
        brand = get_brand(db)
        db.commit()
        # An unissued export used to be titled a bare `Invoice` and downloaded as `document.html` — the
        # same "no identity" defect as the list's em-dash cell, in the browser tab and the Downloads
        # folder (ext#46 review round 1). Both are named from the id now; the document BODY is unchanged.
        html = render_document_html(view_for(doc, brand), standalone=True,
                                    name=document_handle(doc.number, doc.status.value, doc.id))
        name = export_stem(doc.number, doc.kind.value, doc.status.value, doc.id) + ".html"
    return Response(html, mimetype="text/html",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.get("/documents/<uuid:doc_id>/export.pdf")
def export_pdf(doc_id: uuid.UUID):
    """The PDF. 503 when weasyprint is absent — a clear "this install cannot print" beats a 500 or,
    worse, silently handing back HTML with a ``.pdf`` filename."""
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = _load(db, doc_id)
        brand = get_brand(db)
        db.commit()
        pdf = render_document_pdf(view_for(doc, brand),
                                  name=document_handle(doc.number, doc.status.value, doc.id))
        name = export_stem(doc.number, doc.kind.value, doc.status.value, doc.id) + ".pdf"
    if pdf is None:
        return Response(
            "PDF rendering is not available on this install: the optional `weasyprint` dependency is "
            "not installed (`pip install 'cream[pdf]'`). The HTML export is unaffected.",
            status=503, mimetype="text/plain",
        )
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.get("/brand")
def brand_settings():
    """Issuer identity + house style — the letterhead every document is rendered with."""
    cfg = get_config()
    with cfg.session_factory() as db:
        brand = get_brand(db)
        db.commit()
        data = {
            "company_name": brand.company_name or "", "address": brand.address or "",
            "email": brand.email or "", "phone": brand.phone or "", "website": brand.website or "",
            "tax_id": brand.tax_id or "", "logo_data_uri": brand.logo_data_uri or "",
            "accent_color": brand.accent_color or "#0f766e", "font_stack": brand.font_stack or "",
            "default_currency": brand.default_currency or "USD",
            "default_tax_label": brand.default_tax_label or "",
            "default_tax_pct": as_json(pct(brand.default_tax_pct)),
            "payment_instructions": brand.payment_instructions or "",
            "footer_terms": brand.footer_terms or "",
            "default_roe_terms": brand.default_roe_terms or "",
        }
    return render_template("cream/brand.html", brand=data)
