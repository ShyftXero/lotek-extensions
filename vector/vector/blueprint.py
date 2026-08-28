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

from flask import Blueprint, abort, redirect, render_template, request, url_for
from markupsafe import Markup
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from vector._version import __version__
from vector.deps import (
    current_actor_id,
    current_actor_is_admin,
    get_config,
    host_can_write,
    host_setting,
)
from vector.models import Diagram, UserPref
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
        # The ⚙ cog renders only when there is a host identity to scope a preference to (see
        # user_settings) — standalone Vector has one user and nothing to prefer against.
        "vector_has_user_settings": current_actor_id() is not None,
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


# ── per-USER preferences ───────────────────────────────────────────────────────────────────────
#
# The user half of the settings split (lotek-extensions#111): a personal preference crosses no
# privilege boundary, so it lives in Vector's own table behind Vector's own ⚙ cog rather than in the
# host's admin Extensions page. The ADMIN half is the mirror image — declared in
# `lotek-extension.toml` [[settings]], owned/gated/audited by the host, read via `host_setting`.
#
# 🔴 These must never become an authorization input. `visible_diagrams_stmt` above is the IDOR guard
# and is deliberately untouched: a preference filters what is ALREADY visible to you.


def _prefs(db) -> UserPref | None:
    """The current host user's preference row, or None (anonymous / standalone / never saved)."""
    uid = current_actor_id()
    if uid is None:
        return None
    return db.scalars(select(UserPref).where(UserPref.owner_id == uid)).first()


def _save_prefs(db, uid, hide: bool) -> None:
    """Upsert MY preference row. `owner_id` is UNIQUE, and select-then-insert is a check-then-act:
    two concurrent saves (a double-clicked Save, or two gevent workers in prod) both see `None` and
    both insert, so the loser hits the constraint. The constraint is doing its job — the bug was that
    nothing caught it, turning a double-click into a 500. Retry once against the row the winner
    wrote."""
    row = _prefs(db)
    if row is None:
        row = UserPref(owner_id=uid)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            row = _prefs(db)
            if row is None:  # not the collision we expected — don't swallow a real error
                raise
    # ONE assignment, on every path. An earlier version re-queried after the flush and guarded the
    # assignment with `if row is not None`, which had a branch that committed the row WITHOUT the
    # user's choice — a save that reports success and changes nothing.
    row.hide_builtin_diagrams = hide
    db.commit()


@bp.get("/settings")
def user_settings():
    """Vector's own ⚙ page — MY preferences, nobody else's.

    Requires a host identity: without one there is no "my" to scope a preference to, and writing a
    NULL-owner row would make one anonymous session's choice apply to every other. Standalone Vector
    (single local user, no host identity) therefore has no preferences page; that is honest, not a
    gap — it has exactly one user and nothing to distinguish them from.
    """
    if current_actor_id() is None:
        abort(404)
    cfg = get_config()
    with cfg.session_factory() as db:
        row = _prefs(db)
        hide_builtin = bool(row.hide_builtin_diagrams) if row is not None else False
    return render_template("vector/settings.html", hide_builtin_diagrams=hide_builtin,
                           saved=request.args.get("saved") == "1")


@bp.post("/settings")
def user_settings_save():
    """Save MY preferences. Scoped to `current_actor_id()` — the form carries no owner field, so
    there is nothing for a caller to point at someone else's row."""
    uid = current_actor_id()
    if uid is None:
        abort(404)
    hide = str(request.form.get("hide_builtin_diagrams") or "").strip().lower() in ("1", "true", "on")
    cfg = get_config()
    with cfg.session_factory() as db:
        _save_prefs(db, uid, hide)
    return redirect(url_for("vector.user_settings", saved="1"))


@bp.get("/")
@bp.get("/diagrams")
def dashboard():
    cfg = get_config()
    rows = []
    with cfg.session_factory() as db:
        # The preference filters the ALREADY-SCOPED result; it is not folded into
        # visible_diagrams_stmt(), which is the access guard.
        prefs = _prefs(db)
        hide_builtin = bool(prefs.hide_builtin_diagrams) if prefs is not None else False
        for d in db.scalars(visible_diagrams_stmt().order_by(Diagram.updated_at.desc())).all():
            if hide_builtin and d.builtin:
                continue
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
    html = render_deliverable(doc, title=title, footer=host_setting("deliverable_footer", ""))
    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _safe_filename(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_ ") else "-" for c in (name or "attack-path"))
    return (keep.strip().replace(" ", "-") or "attack-path")[:80]
