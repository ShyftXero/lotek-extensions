"""Human-facing UI blueprint — the diagram list + the browser editor, plus the GET export downloads.

Route names are a stable contract used by templates and the host nav: ``vector.dashboard``,
``vector.new_diagram``, ``vector.edit_diagram``, ``vector.api_export_html``, ``vector.api_export_json``.

Access scope (IDOR guard): a diagram is visible to its owner, to any admin, and — for the seeded
read-only ``builtin`` examples — to everyone. Non-owners never see another user's private diagram. The
host's role gate is the write authority; ``vector_can_write`` only drives UI affordances.
"""

from __future__ import annotations

import json
import uuid

from flask import Blueprint, abort, redirect, render_template, url_for
from markupsafe import Markup
from sqlalchemy import or_, select

from vector._version import __version__
from vector.deps import current_actor_id, current_actor_is_admin, get_config, host_can_write
from vector.models import Diagram
from vector.render import json_for_script
from vector.schema import blank_model, normalize

bp = Blueprint("vector", __name__, template_folder="templates", static_folder="static")


def api_base() -> str:
    return get_config().url_prefix.rstrip("/") + "/api"


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "vector_base": cfg.base_template,
        "vector_version": __version__,
        "vector_can_write": host_can_write(),
        "api_base": api_base(),
        "export_html_base": api_base() + "/export.html",
    }


def visible_diagrams_stmt():
    """A SELECT of the diagrams the current actor may see (own + builtin; admins see all)."""
    if current_actor_is_admin():
        return select(Diagram)
    uid = current_actor_id()
    return select(Diagram).where(or_(Diagram.owner_id == uid, Diagram.builtin.is_(True)))


def load_visible_or_404(db, diagram_id: uuid.UUID) -> Diagram:
    row = db.get(Diagram, diagram_id)
    if row is None:
        abort(404)
    if current_actor_is_admin() or row.builtin or row.owner_id == current_actor_id():
        return row
    abort(404)  # 404 not 403 — don't disclose existence of another owner's diagram


def _counts(model_json: str) -> tuple[int, int]:
    try:
        m = json.loads(model_json or "{}")
    except (ValueError, TypeError):
        return 0, 0
    phases = [p for p in (m.get("phases") or []) if not (isinstance(p, dict) and p.get("intro"))]
    return len(phases), len(m.get("nodes") or [])


@bp.get("/")
@bp.get("/diagrams")
def dashboard():
    cfg = get_config()
    rows = []
    with cfg.session_factory() as db:
        for d in db.scalars(visible_diagrams_stmt().order_by(Diagram.updated_at.desc())).all():
            pc, nc = _counts(d.model_json)
            rows.append(
                {
                    "id": d.id, "name": d.name, "builtin": d.builtin,
                    "updated": d.updated_at.strftime("%Y-%m-%d %H:%M") if d.updated_at else "",
                    "phase_count": pc, "node_count": nc,
                }
            )
    return render_template("vector/list.html", diagrams=rows)


@bp.get("/new")
def new_diagram():
    if not host_can_write():
        return redirect(url_for("vector.dashboard"))
    model = blank_model()
    return render_template(
        "vector/editor.html",
        diagram_id="new",
        diagram_name="Untitled attack path",
        model_json=Markup(json_for_script(model)),
    )


@bp.get("/edit/<uuid:diagram_id>")
def edit_diagram(diagram_id: uuid.UUID):
    cfg = get_config()
    with cfg.session_factory() as db:
        d = load_visible_or_404(db, diagram_id)
        model = normalize(json.loads(d.model_json or "{}"))
        name = d.name
    return render_template(
        "vector/editor.html",
        diagram_id=str(diagram_id),
        diagram_name=name,
        model_json=Markup(json_for_script(model)),
    )


@bp.get("/diagrams/<uuid:diagram_id>/export.json")
def api_export_json(diagram_id: uuid.UUID):
    from flask import Response

    cfg = get_config()
    with cfg.session_factory() as db:
        d = load_visible_or_404(db, diagram_id)
        doc = normalize(json.loads(d.model_json or "{}"))
        fname = _safe_filename(d.name) + ".json"
    return Response(
        json.dumps(doc, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@bp.get("/diagrams/<uuid:diagram_id>/export.html")
def api_export_html(diagram_id: uuid.UUID):
    from flask import Response

    from vector.render import render_deliverable

    cfg = get_config()
    with cfg.session_factory() as db:
        d = load_visible_or_404(db, diagram_id)
        doc = json.loads(d.model_json or "{}")
        title = d.name
        fname = _safe_filename(d.name) + ".html"
    html = render_deliverable(doc, title=title)
    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _safe_filename(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_ ") else "-" for c in (name or "attack-path"))
    return (keep.strip().replace(" ", "-") or "attack-path")[:80]
