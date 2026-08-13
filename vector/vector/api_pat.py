"""PAT-scoped MACHINE API for Vector — mounted at ``<url_prefix>/machine`` on its OWN blueprint.

Lets host TOOLS (an agent on a personal access token) drive Vector's attack-path diagrams the host's way
(``Authorization: Bearer lotek_pat_…`` + scope RBAC), the same contract lotek's ``/api/v1`` and
scribble's machine API use. Distinct from the cookie-authed browser API at ``<url_prefix>/api``.

TENANCY (the load-bearing difference from the browser api.py): a machine request has NO session, so the
host's ``current_actor`` (and thus ``vector.deps.current_actor_id``) is None here. This module resolves
the actor from the PAT principal (``host.actor()`` -> ``PatActor`` with ``.id``/``.role``) and applies the
same rule the browser surface does — own diagrams + builtin examples; admins see all; builtin is
read-only — against THAT actor, never the session one.

SECURITY (fail-closed):
  1. ``machine_bp.before_request = host.authenticate`` — every route needs a valid token (503 unmounted).
  2. ``@host.require_scope("read"|"write")`` per route — scope RBAC (and a write token can't out-rank a
     demoted owner: the host's require_pat_scope re-checks the owning user is write-capable).
  3. per-row tenancy is explicit below and never widened.
CSRF: the host exempts this prefix (manifest ``[host] machine_prefix``). Sound ONLY because these routes
accept no cookie — never add a cookie fallback, never widen the prefix over the browser ``/api``.

ID TYPE: diagram ids are ``int`` here because :class:`vector.models.Diagram` is still Integer-keyed, the
same as the browser surface's ``<int:diagram_id>`` routes. lotek's own vendored snapshot of Vector has
since been migrated to UUIDv7 keys; this package has NOT, and a schema migration is not an API port's
business. See ``_actor_owner_id`` for what that costs while mounted, and ``plans/`` for the follow-up.
"""

from __future__ import annotations

import json
import logging

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import or_, select

from vector import host
from vector.api_schemas import CreateDiagramRequest, UpdateDiagramRequest, request_body
from vector.deps import get_config
from vector.models import Diagram
from vector.render import render_deliverable
from vector.schema import normalize

machine_bp = Blueprint("vector_machine", __name__)
machine_bp.before_request(host.authenticate)

_NAME_CAP = 200


def _is_admin(actor) -> bool:
    return actor is not None and str(getattr(actor, "role", "")).lower() == "admin"


def _clean_name(raw, fallback: str = "Untitled attack path") -> str:
    if not isinstance(raw, str):
        return fallback
    return raw.strip()[:_NAME_CAP] or fallback


def _actor_owner_id(actor):
    """The PAT principal's id, if this package can actually store it as an owner.

    ``Diagram.owner_id`` is an ``Integer`` column while lotek's core ``User.id`` is a UUIDv7, so a mounted
    host's principal id does not fit. Degrade LOUDLY rather than binding a UUID into an Integer column:
    return None and warn, exactly as ``vector.deps.current_actor_id`` already does for the browser
    surface. The consequence is a NULL owner (admin-visible only) — the SAME pre-existing limitation the
    cookie surface has today, not one this machine API introduces. It disappears when Vector migrates to
    UUIDv7 keys, at which point this guard accepts ``uuid.UUID`` instead.
    """
    ident = getattr(actor, "id", None)
    if isinstance(ident, int) and not isinstance(ident, bool):
        return ident
    if ident is not None:
        logging.getLogger("vector").warning(
            "machine api: PAT actor id is %s, not an int; diagram owner will be NULL until Vector's "
            "keys are migrated to UUIDv7",
            type(ident).__name__,
        )
    return None


def _visible_stmt(actor):
    """Diagrams the token's user may see: own + builtin; admins see all. NULL-owner rows are visible
    only to admins (never guessed onto a PAT user).

    A principal whose id this package cannot represent gets builtin-only. Comparing against a None uid
    would render as ``owner_id IS NULL`` and LIST exactly the null-owner rows that
    ``_load_visible_or_none`` correctly refuses — list and get must agree.
    """
    if _is_admin(actor):
        return select(Diagram)
    uid = _actor_owner_id(actor)
    if uid is None:
        return select(Diagram).where(Diagram.builtin.is_(True))
    return select(Diagram).where(or_(Diagram.owner_id == uid, Diagram.builtin.is_(True)))


def _load_visible_or_none(db, actor, diagram_id: int) -> Diagram | None:
    row = db.get(Diagram, diagram_id)
    if row is None:
        return None
    uid = _actor_owner_id(actor)
    if _is_admin(actor) or row.builtin or (uid is not None and row.owner_id == uid):
        return row
    return None  # same 404 for missing and not-visible — no existence oracle


def _dict(d: Diagram) -> dict:
    return {"id": d.id, "name": d.name, "builtin": d.builtin}


def _safe_filename(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_ ") else "-" for c in (name or "attack-path"))
    return (keep.strip().replace(" ", "-") or "attack-path")[:80]


@machine_bp.get("/diagrams")
@host.require_scope("read")
def list_diagrams():
    """List attack-path diagrams visible to the token's user (own + builtin; admin sees all)."""
    actor = host.actor()
    with get_config().session_factory() as db:
        rows = db.scalars(_visible_stmt(actor).order_by(Diagram.updated_at.desc())).all()
        return jsonify(diagrams=[_dict(d) for d in rows])


@machine_bp.get("/diagrams/<int:diagram_id>")
@host.require_scope("read")
def get_diagram(diagram_id: int):
    """Fetch one diagram's normalized vector.attackpath/v1 model, if visible to the token's user."""
    actor = host.actor()
    with get_config().session_factory() as db:
        d = _load_visible_or_none(db, actor, diagram_id)
        if d is None:
            return jsonify({"error": "not_found"}), 404
        return jsonify(
            id=d.id, name=d.name, builtin=d.builtin,
            model=normalize(json.loads(d.model_json or "{}")),
        )


@machine_bp.post("/diagrams")
@host.require_scope("write")
@request_body(CreateDiagramRequest)
def create_diagram():
    """Create an attack-path diagram owned by the token's user."""
    actor = host.actor()
    body = request.get_json(silent=True) or {}
    with get_config().session_factory() as db:
        row = Diagram(
            name=_clean_name(body.get("name")),
            model_json=json.dumps(normalize(body.get("model")), ensure_ascii=False),
            owner_id=_actor_owner_id(actor),
            created_by=getattr(actor, "username", None),
        )
        db.add(row)
        db.commit()
        return jsonify(id=row.id, name=row.name), 201


@machine_bp.put("/diagrams/<int:diagram_id>")
@host.require_scope("write")
@request_body(UpdateDiagramRequest)
def update_diagram(diagram_id: int):
    """Update a diagram's name and/or model. Owner or admin only; builtin examples are read-only."""
    actor = host.actor()
    body = request.get_json(silent=True) or {}
    with get_config().session_factory() as db:
        d = _load_visible_or_none(db, actor, diagram_id)
        if d is None:
            return jsonify({"error": "not_found"}), 404
        if d.builtin:
            return jsonify({"error": "forbidden", "detail": "builtin diagrams are read-only"}), 403
        if "name" in body:
            d.name = _clean_name(body.get("name"), d.name)
        if "model" in body:
            d.model_json = json.dumps(normalize(body.get("model")), ensure_ascii=False)
        db.commit()
        return jsonify(id=d.id, name=d.name)


@machine_bp.delete("/diagrams/<int:diagram_id>")
@host.require_scope("write")
def delete_diagram(diagram_id: int):
    """Delete a diagram. Owner or admin only; builtin examples cannot be deleted."""
    actor = host.actor()
    with get_config().session_factory() as db:
        d = _load_visible_or_none(db, actor, diagram_id)
        if d is None:
            return jsonify({"error": "not_found"}), 404
        if d.builtin:
            return jsonify({"error": "forbidden", "detail": "builtin diagrams are read-only"}), 403
        db.delete(d)
        db.commit()
        return jsonify(deleted=diagram_id)


@machine_bp.get("/diagrams/<int:diagram_id>/export.html")
@host.require_scope("read")
def export_diagram(diagram_id: int):
    """Render a saved diagram to a self-contained HTML deliverable (report evidence)."""
    actor = host.actor()
    with get_config().session_factory() as db:
        d = _load_visible_or_none(db, actor, diagram_id)
        if d is None:
            return jsonify({"error": "not_found"}), 404
        html = render_deliverable(json.loads(d.model_json or "{}"), title=d.name)
        fname = _safe_filename(d.name)
    return Response(
        html, mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{fname}.html"'},
    )
