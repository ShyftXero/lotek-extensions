"""PAT-scoped MACHINE API — mounted at ``<url_prefix>/machine`` on its OWN blueprint.

Bearer token + scope RBAC. CSRF-exempt + session-gate-exempt (host manifest declares machine_prefix).

Password-protected pastes: supply the paste password in the ``X-Paste-Password`` request header.
The ``Authorization: Bearer`` header is for PAT authentication; there is no conflict.

A paste the caller may not see is 404, never 403 (INV-TENANCY-01, no existence oracle).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from flask import Blueprint, jsonify, request

from pastel import deps, host, service
from pastel.db import utcnow

machine_bp = Blueprint("pastel_machine", __name__)
_log = logging.getLogger(__name__)

machine_bp.before_request(host.authenticate)


def _principal() -> tuple[uuid.UUID | None, str | None, bool]:
    actor = host.actor()
    ident = getattr(actor, "id", None)
    if not isinstance(ident, uuid.UUID):
        ident = None
    is_admin = actor is not None and str(getattr(actor, "role", "")).lower() == "admin"
    return ident, getattr(actor, "username", None), is_admin


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _denied(exc):
    return jsonify({"error": "forbidden", "detail": str(exc)}), 403


def _bad(msg: str):
    return jsonify({"error": "bad_request", "detail": msg}), 400


def _not_found():
    return jsonify({"error": "not_found"}), 404


@machine_bp.post("/pastes")
@host.require_scope("write")
def machine_create():
    actor_id, username, _ = _principal()
    body = _body()

    expires_at = None
    if "expires_at" in body:
        try:
            expires_at = datetime.fromisoformat(str(body["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return _bad("invalid expires_at: use ISO 8601")
    elif "ttl_seconds" in body:
        try:
            expires_at = utcnow() + timedelta(seconds=int(body["ttl_seconds"]))
        except (ValueError, TypeError):
            return _bad("invalid ttl_seconds: must be a positive integer")

    with deps.get_config().session_factory() as db:
        try:
            paste = service.create(
                db,
                owner_id=actor_id,
                owner_name=username,
                body=body.get("body"),
                title=body.get("title"),
                expires_at=expires_at,
                read_limit=body.get("read_limit"),
                password=body.get("password"),
                standalone=False,
                audit_fn=deps.host_audit(),
            )
        except service.Denied as exc:
            return _denied(exc)
        except service.Invalid as exc:
            return _bad(str(exc))
        except ValueError:
            _log.warning("pastel/machine: unexpected ValueError on %s", request.path, exc_info=True)
            return _bad("invalid request")
    return jsonify({"paste": service.to_dict(paste)}), 201


@machine_bp.get("/pastes/<slug>")
@host.require_scope("read")
def machine_read(slug: str):
    actor_id, _, _ = _principal()
    paste_password = request.headers.get("X-Paste-Password") or None

    with deps.get_config().session_factory() as db:
        paste = service.load_by_slug(db, slug)
        if paste is None:
            return _not_found()
        is_owner = paste.owner_id is not None and actor_id == paste.owner_id
        try:
            service.access_and_count(
                db, paste,
                password=paste_password,
                is_owner=is_owner,
                audit_fn=deps.host_audit(),
            )
        except (service.Gone, service.NotFound):
            return _not_found()
        except service.PasswordRequired:
            return jsonify({
                "error": "password_required",
                "detail": "include the paste password in the X-Paste-Password request header",
            }), 401
        except service.WrongPassword:
            return jsonify({"error": "wrong_password"}), 401
    return jsonify({"paste": service.to_dict(paste)})


@machine_bp.delete("/pastes/<slug>")
@host.require_scope("write")
def machine_delete(slug: str):
    actor_id, _, is_admin = _principal()
    with deps.get_config().session_factory() as db:
        try:
            service.delete_paste(
                db, slug,
                owner_id=actor_id,
                is_admin=is_admin,
                audit_fn=deps.host_audit(),
            )
        except service.NotFound:
            return _not_found()
        except service.Denied as exc:
            return _denied(exc)
    return jsonify({"deleted": True})
