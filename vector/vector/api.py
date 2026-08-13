"""JSON API blueprint (mounted at ``<url_prefix>/api``).

Cookie-authed browser surface: CRUD over diagrams, JSON import, and an export-of-unsaved-state endpoint.
When mounted in a host that enforces CSRF (lotek), these routes stay CSRF-protected (the editor sends the
``X-CSRFToken`` header); the host's role gate blocks viewers on mutating methods. We additionally check
``host_can_write`` and per-row ownership here (defense in depth + the standalone case, which has no host
gate). Writes are owner-scoped: only the owner or an admin may modify a row, and the seeded ``builtin``
examples are read-only (duplicate, don't edit).
"""

from __future__ import annotations

import json
import uuid

from flask import Blueprint, Response, abort, jsonify, request
from sqlalchemy import select

from vector._version import __version__
from vector.blueprint import load_visible_or_404, visible_diagrams_stmt
from vector.deps import (
    current_actor_id,
    current_actor_is_admin,
    current_actor_username,
    get_config,
    host_can_write,
)
from vector.models import Diagram
from vector.render import render_deliverable
from vector.schema import normalize

api_bp = Blueprint("vector_api", __name__)

_NAME_CAP = 200


def _require_write():
    if not host_can_write():
        abort(403)


def _require_owner(row: Diagram):
    """Only the owner or an admin may modify; builtin examples are never modified/deleted."""
    if row.builtin:
        abort(403)
    if current_actor_is_admin() or row.owner_id == current_actor_id():
        return
    abort(403)


def _clean_name(raw, fallback: str = "Untitled attack path") -> str:
    if not isinstance(raw, str):
        return fallback
    name = raw.strip()[:_NAME_CAP]
    return name or fallback


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@api_bp.get("/health")
def health():
    cfg = get_config()
    with cfg.session_factory() as db:
        n = db.scalar(select(Diagram.id)) is not None
    return jsonify(status="ok", version=__version__, has_diagrams=bool(n))


@api_bp.get("/diagrams")
def list_diagrams():
    cfg = get_config()
    with cfg.session_factory() as db:
        rows = db.scalars(visible_diagrams_stmt().order_by(Diagram.updated_at.desc())).all()
        return jsonify(
            diagrams=[{"id": d.id, "name": d.name, "builtin": d.builtin} for d in rows]
        )


@api_bp.get("/diagrams/<uuid:diagram_id>")
def get_diagram(diagram_id: uuid.UUID):
    cfg = get_config()
    with cfg.session_factory() as db:
        d = load_visible_or_404(db, diagram_id)
        return jsonify(
            id=d.id, name=d.name, builtin=d.builtin,
            model=normalize(json.loads(d.model_json or "{}")),
        )


def _create(db, name: str, model, *, source_job_id: uuid.UUID | None = None) -> Diagram:
    doc = normalize(model)
    row = Diagram(
        name=name,
        model_json=json.dumps(doc, ensure_ascii=False),
        owner_id=current_actor_id(),
        created_by=current_actor_username(),
        source_job_id=source_job_id,
    )
    db.add(row)
    db.commit()
    return row


@api_bp.post("/diagrams")
@api_bp.post("/import")
def create_diagram():
    _require_write()
    body = _body()
    cfg = get_config()
    with cfg.session_factory() as db:
        row = _create(db, _clean_name(body.get("name")), body.get("model"))
        return jsonify(id=row.id, name=row.name), 201


@api_bp.put("/diagrams/<uuid:diagram_id>")
def update_diagram(diagram_id: uuid.UUID):
    _require_write()
    body = _body()
    cfg = get_config()
    with cfg.session_factory() as db:
        d = load_visible_or_404(db, diagram_id)
        _require_owner(d)
        if "name" in body:
            d.name = _clean_name(body.get("name"), d.name)
        if "model" in body:
            d.model_json = json.dumps(normalize(body.get("model")), ensure_ascii=False)
        db.commit()
        return jsonify(id=d.id, name=d.name)


@api_bp.delete("/diagrams/<uuid:diagram_id>")
def delete_diagram(diagram_id: uuid.UUID):
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        d = load_visible_or_404(db, diagram_id)
        _require_owner(d)
        db.delete(d)
        db.commit()
        return jsonify(deleted=str(diagram_id))


@api_bp.post("/diagrams/<uuid:diagram_id>/duplicate")
def duplicate_diagram(diagram_id: uuid.UUID):
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        d = load_visible_or_404(db, diagram_id)  # may duplicate any VISIBLE diagram (incl. builtin)
        row = _create(db, _dup_name(d.name), json.loads(d.model_json or "{}"))
        return jsonify(id=row.id, name=row.name), 201


def _dup_name(name: str) -> str:
    return _clean_name((name or "Untitled") + " (copy)")


@api_bp.post("/export.html")
def export_html_unsaved():
    """Export a (possibly unsaved) editor model to a self-contained HTML deliverable."""
    body = _body()
    model = body.get("model")
    title = body.get("title") if isinstance(body.get("title"), str) else None
    html = render_deliverable(model, title=title)
    fname = _safe_filename(title or "attack-path") + ".html"
    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _safe_filename(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_ ") else "-" for c in (name or "attack-path"))
    return (keep.strip().replace(" ", "-") or "attack-path")[:80]
