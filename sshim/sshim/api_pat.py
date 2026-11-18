"""PAT-scoped MACHINE API — mounted at ``<url_prefix>/machine`` on its OWN blueprint.

How an AGENT drives sshim: list its running pivots, RE-FETCH a session's full plan (both legs, every
applicable renderer — the same ``render_plan`` output the browser shows), mint a new pivot, kill one.
Bearer token + scope RBAC, the contract lotek's ``/api/v1`` and the sibling extensions use.

TENANCY: a machine request has no session, so identity comes from the PAT principal (``host.actor()``)
and is fed into the SAME ``sshim.service`` functions the browser surface uses — the visibility rule has
exactly one implementation. A session a token may not see is **404, never 403** (INV-TENANCY-01).

The pivot's password is returned in exactly two places and never in a list:

* ``POST /sessions`` (the mint response) — the identity was just generated for this caller;
* ``GET /sessions/<id>`` (the owner's/admin's re-fetch) — where ``service.plan_for`` unseals it.

``service.session_to_dict`` (what ``GET /sessions`` serialises) has no ``ssh_password`` key at all, so
the wide list surface cannot leak a credential it was never handed.
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, jsonify, request

from sshim import host
from sshim.deps import get_config, host_audit, is_standalone, max_ttl_minutes, secret_key_path
from sshim.service import (
    Denied,
    Invalid,
    SessionDead,
    kill_session,
    load_visible,
    mint_session,
    now_utc,
    parse_kill_at,
    plan_for,
    plan_to_dict,
    profile_from_fields,
    session_to_dict,
    visible_sessions,
)

machine_bp = Blueprint("sshim_machine", __name__)
_log = logging.getLogger(__name__)

machine_bp.before_request(host.authenticate)


def _principal() -> tuple[uuid.UUID | None, bool]:
    """(id, is_admin) for the token's user. A non-UUID id is treated as no identity: the owner filter
    then matches nothing, which is the fail-closed direction."""
    actor = host.actor()
    ident = getattr(actor, "id", None)
    if not isinstance(ident, uuid.UUID):
        ident = None
    is_admin = actor is not None and str(getattr(actor, "role", "")).lower() == "admin"
    return ident, is_admin


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _not_found():
    return jsonify({"error": "not_found", "detail": "no such session"}), 404


@machine_bp.get("/sessions")
@host.require_scope("read")
def list_sessions():
    """The running-pivots list for this token's user (or all, for an admin)."""
    actor_id, is_admin = _principal()
    now = now_utc()
    with get_config().session_factory() as db:
        rows = visible_sessions(db, owner_id=actor_id, is_admin=is_admin, now=now)
        return jsonify(sessions=[session_to_dict(r, now=now) for r in rows])


@machine_bp.get("/sessions/<uuid:session_id>")
@host.require_scope("read")
def get_session(session_id: uuid.UUID):
    """RE-FETCH one pivot's FULL plan — both legs, every applicable renderer, regenerated from storage."""
    actor_id, is_admin = _principal()
    with get_config().session_factory() as db:
        row = load_visible(db, session_id, owner_id=actor_id, is_admin=is_admin)
        if row is None:
            return _not_found()
        try:
            plan = plan_for(row, secret_key_path=secret_key_path())
        except SessionDead as exc:
            # Killed (including a reached kill_at): the commands are no longer served. 409, not 404 —
            # the caller may see the row, it is the PLAN that is refused.
            return jsonify({"error": "session_dead", "detail": str(exc)}), 409
        return jsonify(session=session_to_dict(row), plan=plan_to_dict(plan))


@machine_bp.post("/sessions")
@host.require_scope("write")
def create_session():
    """Mint a pivot. Returns this session's OWN generated identity + the sealed-at-rest password."""
    actor_id, _is_admin = _principal()
    body = _body()
    with get_config().session_factory() as db:
        try:
            prof = profile_from_fields(
                name=body.get("name") or body.get("rendezvous_host") or "pivot",
                topology=body.get("topology"),
                rendezvous_host=body.get("rendezvous_host"),
                rendezvous_port=body.get("rendezvous_port", 22),
                rendezvous_user=body.get("rendezvous_user"),
                anchor_host=body.get("anchor_host"),
                anchor_port=body.get("anchor_port", 22),
                anchor_user=body.get("anchor_user"),
                cidr=body.get("cidr", ""),
            )
            row, secret = mint_session(
                db, prof,
                owner_id=actor_id, standalone=is_standalone(),
                ttl_minutes=int(body.get("ttl_minutes", 30) or 30),
                max_ttl=max_ttl_minutes(),
                secret_key_path=secret_key_path(),
                kill_at=parse_kill_at(body.get("kill_at")),
                host_audit=host_audit(),
            )
        except Invalid as exc:
            return jsonify({"error": "bad_request", "detail": str(exc)}), 400
        except Denied as exc:
            return jsonify({"error": "forbidden", "detail": str(exc)}), 403
        except (TypeError, ValueError):
            return jsonify({"error": "bad_request", "detail": "invalid request"}), 400
        return jsonify(
            session=session_to_dict(row),
            plan=plan_to_dict(plan_for(row, secret_key_path=secret_key_path())),
            ssh_user=row.ssh_user,
            ssh_password=secret,
            secret_note=(
                "Stored sealed on this instance so its owner can re-fetch it from another box. It is "
                "never on the command line; paste it at the ssh prompt."
            ),
        ), 201


@machine_bp.post("/sessions/<uuid:session_id>/kill")
@host.require_scope("write")
def kill_session_api(session_id: uuid.UUID):
    actor_id, is_admin = _principal()
    with get_config().session_factory() as db:
        row = load_visible(db, session_id, owner_id=actor_id, is_admin=is_admin)
        if row is None:
            return _not_found()
        try:
            kill_session(db, row, owner_id=actor_id, is_admin=is_admin, host_audit=host_audit())
        except Denied as exc:
            return jsonify({"error": "forbidden", "detail": str(exc)}), 403
        return jsonify(status="killed", id=str(session_id))
