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

ID TYPE: diagram ids are ``uuid.UUID`` here — :class:`vector.models.Diagram` is UUIDv7-keyed (matching
lotek's core keys), the same as the browser surface's ``<uuid:diagram_id>`` routes. The PAT principal's
id is likewise a ``uuid.UUID`` and is stored as the owner; see ``_actor_owner_id`` for how a non-UUID
principal id degrades loudly to a NULL owner.
"""

from __future__ import annotations

import json
import logging
import uuid

from flask import Blueprint, Response, jsonify, request

from vector import access, host
from vector.api_schemas import CreateDiagramRequest, UpdateDiagramRequest, request_body
from vector.deps import (
    get_config,
    host_audit,
    host_can_operate_on,
    host_setting,
)
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
    """The PAT principal's id, if it is a ``uuid.UUID`` that can be stored as an owner.

    lotek's core keys ``User`` on UUIDv7 (v2), so a mounted host's principal id is a ``uuid.UUID`` and is
    stored directly as ``Diagram.owner_id`` (a ``Uuid`` column). A non-UUID principal id (a stub/standalone
    principal, or a legacy int) cannot be bound into that column, so degrade LOUDLY — return None and warn,
    exactly as ``vector.deps.current_actor_id`` does for the browser surface — rather than silently
    attributing the row to the wrong owner. The consequence is a NULL owner, visible only to admins.
    """
    ident = getattr(actor, "id", None)
    if isinstance(ident, uuid.UUID):
        return ident
    if ident is not None:
        logging.getLogger("vector").warning(
            "machine api: PAT actor id is %s, not a uuid.UUID; diagram owner will be NULL",
            type(ident).__name__,
        )
    return None


def _visible_stmt(actor):
    """Diagrams the token's user may see — engagement-scoped, then owner-scoped for unbound rows. Routes
    through the SAME ``vector.access`` seam the cookie surface uses, so the two cannot drift; the only
    difference is that the PAT actor's ``(is_admin, owner_id)`` is resolved from the bearer principal."""
    return access.visible_diagrams_stmt(
        is_admin=_is_admin(actor), owner_id=_actor_owner_id(actor))


def _load_visible_or_none(db, actor, diagram_id: uuid.UUID) -> Diagram | None:
    row = db.get(Diagram, diagram_id)
    if row is None:
        return None
    if access.diagram_visible(row, is_admin=_is_admin(actor), owner_id=_actor_owner_id(actor)):
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


@machine_bp.get("/diagrams/<uuid:diagram_id>")
@host.require_scope("read")
def get_diagram(diagram_id: uuid.UUID):
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


def _parse_engagement_id(body):
    """A create body's optional ``engagement_id`` (the tenancy binding). Adopting it from the request is
    sound only because create gates it with ``can_operate_on`` right after (INV-TENANCY-05); every later
    read/write re-derives the key from the stored row. Returns (ok, value): (False, response) on a bad
    UUID so the caller can early-return the 400."""
    raw = body.get("engagement_id")
    if raw in (None, ""):
        return True, None
    try:
        return True, uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return False, (jsonify({"error": "bad_request", "detail": "engagement_id must be a UUID"}), 400)


@machine_bp.post("/diagrams")
@host.require_scope("write")
@request_body(CreateDiagramRequest)
def create_diagram():
    """Create an attack-path diagram. Bind it to an engagement (``engagement_id``) the token holds an
    operator capability on, or leave it unbound (owner-scoped)."""
    actor = host.actor()
    body = request.get_json(silent=True) or {}
    ok, eid = _parse_engagement_id(body)
    if not ok:
        return eid
    if eid is not None and not host_can_operate_on(eid):
        return jsonify({"error": "forbidden",
                        "detail": "you are not an operator on that engagement"}), 403
    with get_config().session_factory() as db:
        row = Diagram(
            name=_clean_name(body.get("name")),
            model_json=json.dumps(normalize(body.get("model")), ensure_ascii=False),
            owner_id=_actor_owner_id(actor),
            created_by=getattr(actor, "username", None),
            engagement_id=eid,
        )
        db.add(row)
        db.flush()
        host_audit(db, "create", subject_type="vector_diagram", subject_id=row.id,
                   after={"name": row.name, "engagement_id": str(eid) if eid else None})
        db.commit()
        return jsonify(id=row.id, name=row.name), 201


@machine_bp.put("/diagrams/<uuid:diagram_id>")
@host.require_scope("write")
@request_body(UpdateDiagramRequest)
def update_diagram(diagram_id: uuid.UUID):
    """Update a diagram's name and/or model. Owner or admin only; builtin examples are read-only."""
    actor = host.actor()
    body = request.get_json(silent=True) or {}
    with get_config().session_factory() as db:
        d = _load_visible_or_none(db, actor, diagram_id)
        if d is None:
            return jsonify({"error": "not_found"}), 404
        if d.builtin:
            return jsonify({"error": "forbidden", "detail": "builtin diagrams are read-only"}), 403
        if not access.diagram_writable(d, is_admin=_is_admin(actor), owner_id=_actor_owner_id(actor)):
            # Viewable but not writable: an observer membership on the engagement (INV-TENANCY-05).
            return jsonify({"error": "forbidden", "detail": "you may not modify this diagram"}), 403
        before = {"name": d.name}
        if "name" in body:
            d.name = _clean_name(body.get("name"), d.name)
        if "model" in body:
            d.model_json = json.dumps(normalize(body.get("model")), ensure_ascii=False)
        host_audit(db, "update", subject_type="vector_diagram", subject_id=d.id,
                   before=before, after={"name": d.name})
        db.commit()
        return jsonify(id=d.id, name=d.name)


@machine_bp.delete("/diagrams/<uuid:diagram_id>")
@host.require_scope("write")
def delete_diagram(diagram_id: uuid.UUID):
    """Delete a diagram. Owner or admin only; builtin examples cannot be deleted."""
    actor = host.actor()
    with get_config().session_factory() as db:
        d = _load_visible_or_none(db, actor, diagram_id)
        if d is None:
            return jsonify({"error": "not_found"}), 404
        if d.builtin:
            return jsonify({"error": "forbidden", "detail": "builtin diagrams are read-only"}), 403
        if not access.diagram_writable(d, is_admin=_is_admin(actor), owner_id=_actor_owner_id(actor)):
            return jsonify({"error": "forbidden", "detail": "you may not modify this diagram"}), 403
        eid = str(d.engagement_id) if d.engagement_id else None
        host_audit(db, "delete", subject_type="vector_diagram", subject_id=d.id,
                   before={"name": d.name, "engagement_id": eid})
        db.delete(d)
        db.commit()
        return jsonify(deleted=str(diagram_id))


@machine_bp.get("/diagrams/<uuid:diagram_id>/export.html")
@host.require_scope("read")
def export_diagram(diagram_id: uuid.UUID):
    """Render a saved diagram to a self-contained HTML deliverable (report evidence)."""
    actor = host.actor()
    with get_config().session_factory() as db:
        d = _load_visible_or_none(db, actor, diagram_id)
        if d is None:
            return jsonify({"error": "not_found"}), 404
        html = render_deliverable(
            json.loads(d.model_json or "{}"), title=d.name, footer=host_setting("deliverable_footer", ""),
        )
        fname = _safe_filename(d.name)
    return Response(
        html, mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{fname}.html"'},
    )
