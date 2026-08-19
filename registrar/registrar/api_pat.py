"""PAT-scoped MACHINE API for Registrar — mounted at ``<url_prefix>/machine`` on its OWN blueprint.

Lets host TOOLS (an agent on a personal access token) drive Registrar the host's way (Bearer + scope
RBAC), the same contract lotek's ``/api/v1`` and scribble's machine API use. Distinct from the
cookie-authed browser API at ``<url_prefix>/api``.

CONFIRM-TIER IS SESSION-ONLY (INV-EXT-02) — the load-bearing security line:
  * ``POST /machine/action`` mirrors the browser ``/action``: a DIRECT-tier verb runs inline; a
    CONFIRM-tier verb is STAGED (202) and can never execute from here.
  * there is deliberately NO ``/machine/staged/<id>/approve`` route. Execution of a staged action
    requires an interactive session with a different confirmer (the ``approve`` service checks
    ``is_interactive``, which is False for any PAT). Omitting the route makes "a PAT stages, a human
    approves" explicit at the surface, not merely enforced a layer down.

TENANCY: a machine request has no session, so ``registrar.deps.current_actor_*`` are None here. Identity
comes from the PAT principal (``host.actor()``); engagement visibility comes from the principal-based
``host_visible_engagement_ids`` hook (correct for a PAT). SECURITY otherwise as scribble/vector:
``before_request`` authenticates, ``@host.require_scope`` gates each route, the prefix is CSRF/session
exempt only because it takes no cookie.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from registrar import host
from registrar.api_schemas import ActionRequest, request_body
from registrar.deps import get_config, host_audit, host_visible_engagement_ids
from registrar.enums import Tier
from registrar.models import AuditRecord
from registrar.service import (
    ConfirmationRequired,
    execute_direct,
    stage,
    tier_of,
    visible_domains,
    visible_servers,
    visible_staged,
)

machine_bp = Blueprint("registrar_machine", __name__)
machine_bp.before_request(host.authenticate)


def _is_admin(actor) -> bool:
    return actor is not None and str(getattr(actor, "role", "")).lower() == "admin"


@machine_bp.get("/servers")
@host.require_scope("read")
def list_servers():
    """List infrastructure servers visible to the token's user (engagement-scoped; admin sees all)."""
    actor = host.actor()
    with get_config().session_factory() as db:
        rows = visible_servers(db, visible_engagement_ids=host_visible_engagement_ids(),
                               is_admin=_is_admin(actor))
        return jsonify(servers=[
            {"id": str(s.id), "kind": s.kind.value, "state": s.state.value, "name": s.name,
             "provider": s.provider, "ip": s.ip, "role": s.role,
             "engagement_id": str(s.engagement_id) if s.engagement_id else None}
            for s in rows
        ])


@machine_bp.get("/domains")
@host.require_scope("read")
def list_domains():
    """List domains visible to the token's user (engagement-scoped by checkout; admin sees all)."""
    with get_config().session_factory() as db:
        rows = visible_domains(db, visible_engagement_ids=host_visible_engagement_ids(),
                               is_admin=_is_admin(host.actor()))
        return jsonify(domains=[
            {"id": str(d.id), "name": d.name, "provider": d.provider, "registered": d.registered,
             "checked_out_to": str(d.checked_out_to) if d.checked_out_to else None}
            for d in rows
        ])


@machine_bp.post("/action")
@host.require_scope("write")
@request_body(ActionRequest)
def action():
    """Direct-tier verb -> run inline (200). Confirm-tier verb -> STAGE only (202).

    A confirm-tier verb can NEVER execute via the machine API — approval needs an interactive session by
    a different user (INV-EXT-02). An agent stages; a human approves in the dashboard.
    """
    actor = host.actor()
    body = request.get_json(silent=True) or {}
    verb = str(body.get("verb") or "")
    if not verb:
        return jsonify({"error": "bad_request", "detail": "verb is required"}), 400
    provider = str(body.get("provider") or "null")
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    with get_config().session_factory() as db:
        if tier_of(verb) is Tier.confirm:
            row = stage(db, verb=verb, provider=provider, args=args,
                        initiator_id=getattr(actor, "id", None),
                        actor=getattr(actor, "username", None), host_audit=host_audit())
            return jsonify(
                status="staged", staged_id=str(row.id),
                note="confirm-tier: staged only; a human approves it in the dashboard (a PAT cannot approve)",
            ), 202
        try:
            result = execute_direct(db, verb=verb, provider=provider, args=args,
                                    actor=getattr(actor, "username", None), host_audit=host_audit())
        except ConfirmationRequired:
            return jsonify({"error": "conflict", "detail": "confirmation required"}), 409
        except ValueError as e:
            return jsonify({"error": "bad_request", "detail": str(e)}), 400
        return jsonify(status=result.status, detail=result.detail), 200


@machine_bp.get("/staged")
@host.require_scope("read")
def list_staged():
    """List pending staged (confirm-tier) actions visible to the token's user (engagement-scoped;
    admin sees org-level/unbound ones)."""
    with get_config().session_factory() as db:
        rows = visible_staged(db, visible_engagement_ids=host_visible_engagement_ids(),
                              is_admin=_is_admin(host.actor()))
        return jsonify(staged=[
            {"id": str(s.id), "verb": s.verb, "provider": s.provider,
             "initiator_id": str(s.initiator_id) if s.initiator_id else None,
             "created_at": s.created_at.isoformat()}
            for s in rows
        ])


@machine_bp.get("/audit")
@host.require_scope("read")
def audit():
    """The most recent registrar audit records (verb, provider, tier, result) — ADMIN-ONLY.

    ``AuditRecord`` carries NO engagement_id column and the trail spans every engagement's actions, so
    it cannot be engagement-scoped row-by-row (INV-TENANCY-06). A non-admin token gets an empty list
    (consistent with the sibling scoped lists) rather than a cross-tenant action log.
    """
    if not _is_admin(host.actor()):
        return jsonify(audit=[])
    with get_config().session_factory() as db:
        rows = db.scalars(select(AuditRecord).order_by(AuditRecord.at.desc()).limit(50)).all()
        return jsonify(audit=[
            {"at": a.at.isoformat(), "actor": a.actor, "verb": a.verb, "provider": a.provider,
             "tier": a.tier, "result": a.result, "detail": a.detail}
            for a in rows
        ])
