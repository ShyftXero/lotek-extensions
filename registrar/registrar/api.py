"""JSON API blueprint (mounted at ``<url_prefix>/api``).

Cookie-authed human surface. Confirm-tier verbs (outward effect) can only be STAGED here; execution
happens exclusively from ``/staged/<id>/approve``, which requires an interactive session, a confirmer
different from the initiator, and live write authorization (INV-EXT-02). Direct-tier verbs run inline.
"""

from __future__ import annotations

import uuid

from flask import Blueprint, abort, jsonify, request
from sqlalchemy import select

from registrar._version import __version__
from registrar.deps import (
    current_actor_id,
    current_actor_is_admin,
    current_actor_username,
    get_config,
    host_audit,
    host_can_operate_on,
    host_can_write,
    host_is_interactive,
    host_visible_engagement_ids,
)
from registrar.enums import Tier
from registrar.models import AuditRecord, StagedAction
from registrar.service import (
    ApprovalDenied,
    ConfirmationRequired,
    approve,
    execute_direct,
    stage,
    tier_of,
    visible_domains,
    visible_servers,
    visible_staged,
)

api_bp = Blueprint("registrar_api", __name__)


def _require_write():
    if not host_can_write():
        abort(403)


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@api_bp.get("/health")
def health():
    return jsonify(status="ok", version=__version__)


@api_bp.get("/servers")
def list_servers():
    cfg = get_config()
    with cfg.session_factory() as db:
        rows = visible_servers(db, visible_engagement_ids=host_visible_engagement_ids(),
                               is_admin=current_actor_is_admin())
        return jsonify(servers=[
            {"id": str(s.id), "kind": s.kind.value, "state": s.state.value, "name": s.name,
             "provider": s.provider, "ip": s.ip, "role": s.role,
             "engagement_id": str(s.engagement_id) if s.engagement_id else None}
            for s in rows
        ])


@api_bp.get("/domains")
def list_domains():
    cfg = get_config()
    with cfg.session_factory() as db:
        rows = visible_domains(db, visible_engagement_ids=host_visible_engagement_ids(),
                               is_admin=current_actor_is_admin())
        return jsonify(domains=[
            {"id": str(d.id), "name": d.name, "provider": d.provider, "registered": d.registered,
             "checked_out_to": str(d.checked_out_to) if d.checked_out_to else None}
            for d in rows
        ])


@api_bp.post("/action")
def action():
    """Direct-tier verb -> run inline (200). Confirm-tier verb -> STAGE it (202) — it can NEVER execute
    from here; approval is a separate, gated endpoint."""
    _require_write()
    body = _body()
    verb = str(body.get("verb") or "")
    if not verb:
        abort(400, "verb is required")
    provider = str(body.get("provider") or "null")
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    cfg = get_config()
    with cfg.session_factory() as db:
        if tier_of(verb) is Tier.confirm:
            row = stage(db, verb=verb, provider=provider, args=args, initiator_id=current_actor_id(),
                        actor=current_actor_username(), host_audit=host_audit())
            return jsonify(status="staged", staged_id=str(row.id),
                           note="confirm-tier: approve at /staged/<id>/approve as a different user"), 202
        try:
            result = execute_direct(db, verb=verb, provider=provider, args=args,
                                    actor=current_actor_username(), host_audit=host_audit())
        except ConfirmationRequired as e:
            abort(409, str(e))
        except ValueError as e:
            abort(400, str(e))
        return jsonify(status=result.status, detail=result.detail), 200


@api_bp.get("/staged")
def list_staged():
    cfg = get_config()
    with cfg.session_factory() as db:
        rows = visible_staged(db, visible_engagement_ids=host_visible_engagement_ids(),
                              is_admin=current_actor_is_admin())
        return jsonify(staged=[
            {"id": str(s.id), "verb": s.verb, "provider": s.provider,
             "initiator_id": str(s.initiator_id) if s.initiator_id else None,
             "created_at": s.created_at.isoformat()}
            for s in rows
        ])


@api_bp.post("/staged/<uuid:staged_id>/approve")
def approve_staged(staged_id: uuid.UUID):
    """Execute a staged confirm-tier action. Server-side gate (INV-EXT-02): interactive session, a
    confirmer different from the initiator, and live write authorization."""
    _require_write()
    cfg = get_config()
    with cfg.session_factory() as db:
        staged = db.get(StagedAction, staged_id)
        if staged is None:
            abort(404)
        try:
            result = approve(db, staged, confirmer_id=current_actor_id(),
                             confirmer_name=current_actor_username(),
                             is_interactive=host_is_interactive(), can_write=host_can_write(),
                             can_operate_on=host_can_operate_on, host_audit=host_audit())
        except ApprovalDenied as e:
            abort(403, str(e))
        except ConfirmationRequired as e:
            abort(409, str(e))
        return jsonify(status=result.status, detail=result.detail), 200


@api_bp.get("/audit")
def audit():
    # ADMIN-ONLY (INV-TENANCY-06). The registrar audit trail spans every engagement's actions and
    # ``AuditRecord`` carries NO engagement_id column, so it cannot be engagement-scoped row-by-row.
    # Rather than leak a cross-tenant action log, restrict it: a non-admin gets an empty list (200),
    # consistent with how the sibling list endpoints scope by returning fewer rows. Standalone (no host
    # actor) counts as admin, so single-user REGISTRAR still sees its own trail.
    if not current_actor_is_admin():
        return jsonify(audit=[])
    cfg = get_config()
    with cfg.session_factory() as db:
        rows = db.scalars(select(AuditRecord).order_by(AuditRecord.at.desc()).limit(50)).all()
        return jsonify(audit=[
            {"at": a.at.isoformat(), "actor": a.actor, "verb": a.verb, "provider": a.provider,
             "tier": a.tier, "result": a.result, "detail": a.detail}
            for a in rows
        ])
