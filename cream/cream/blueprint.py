"""Human-facing UI blueprint — the document list + a single-document view/export.

Route names are a stable contract used by templates and the host nav: ``cream.dashboard``,
``cream.view_document``, ``cream.export_html``.
"""

from __future__ import annotations

import uuid

from flask import Blueprint, Response, abort, render_template
from sqlalchemy import select

from cream._version import __version__
from cream.deps import get_config, host_can_write, host_visible_engagement_ids
from cream.models import Document
from cream.render import render_document_html
from cream.service import totals

bp = Blueprint("cream", __name__, template_folder="templates")


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "cream_base": cfg.base_template,
        "cream_version": __version__,
        "cream_can_write": host_can_write(),
        "api_base": cfg.url_prefix.rstrip("/") + "/api",
    }


@bp.get("/")
def dashboard():
    cfg = get_config()
    rows = []
    vis = host_visible_engagement_ids()
    with cfg.session_factory() as db:
        for d in db.scalars(select(Document).order_by(Document.created_at.desc())).all():
            if vis is not None and d.engagement_id not in vis:
                continue  # read-scope to the actor's engagements
            rows.append({
                "id": str(d.id), "kind": d.kind.value, "status": d.status.value,
                "number": d.number or "—", "title": d.title,
                "total": totals(d)["total"], "currency": d.currency,
            })
    return render_template("cream/list.html", documents=rows)


@bp.get("/documents/<uuid:doc_id>")
def view_document(doc_id: uuid.UUID):
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            abort(404)
        vis = host_visible_engagement_ids()
        if vis is not None and doc.engagement_id not in vis:
            abort(404)
        html = render_document_html(doc)
    return render_template("cream/view.html", doc_html=html, doc_id=str(doc_id))


@bp.get("/documents/<uuid:doc_id>/export.html")
def export_html(doc_id: uuid.UUID):
    cfg = get_config()
    with cfg.session_factory() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            abort(404)
        vis = host_visible_engagement_ids()
        if vis is not None and doc.engagement_id not in vis:
            abort(404)
        html = render_document_html(doc, standalone=True)
        name = (doc.number or "document") + ".html"
    return Response(html, mimetype="text/html",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
