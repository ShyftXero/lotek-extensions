"""PAT-scoped MACHINE API — mounted at ``<url_prefix>/machine`` on its OWN blueprint.

How a RUNNER posts a paste. Bearer token + scope RBAC, the same contract lotek's ``/api/v1`` and the
sibling extensions' machine APIs use; distinct from the cookie-authed browser page at ``<url_prefix>/``.

TENANCY: a machine request has no session, so ``pastel.deps.current_actor_*`` are all None here.
Identity comes from the PAT principal (``host.actor()``) and is fed into the SAME ``pastel.service``
functions the browser surface uses — the visibility rule has exactly one implementation. A paste the
token may not see is **404, never 403** (INV-TENANCY-01).

CONTENT FETCH is deliberately NOT here. A runner reads a paste back the same way anyone does: the public
GUID URL (``/pastel/p/<guid>``), supplying a password, if any, as HTTP Basic. That keeps the read gate
(expiry / read-count / password) in one place instead of forking a second, authed content path.
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, jsonify, request, url_for

from pastel import host
from pastel.api_schemas import CreatePasteRequest, request_body
from pastel.deps import get_config, host_audit
from pastel.service import (
    Denied,
    Invalid,
    create,
    delete_paste,
    load_own,
    to_dict,
    visible_pastes,
)

machine_bp = Blueprint("pastel_machine", __name__)
_log = logging.getLogger(__name__)

machine_bp.before_request(host.authenticate)


def _principal() -> tuple[uuid.UUID | None, str | None, bool]:
    """(id, username, is_admin) for the token's user. A non-UUID id is treated as no identity: the owner
    filter then matches nothing, which is the fail-closed direction."""
    actor = host.actor()
    ident = getattr(actor, "id", None)
    if not isinstance(ident, uuid.UUID):
        ident = None
    is_admin = actor is not None and str(getattr(actor, "role", "")).lower() == "admin"
    return ident, getattr(actor, "username", None), is_admin


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _denied(exc: Exception):
    return jsonify({"error": "forbidden", "detail": str(exc)}), 403


def _bad(exc: Invalid):
    """400 quoting the message — only ever called with ``Invalid``, whose messages this extension writes
    itself and are safe to show a caller."""
    return jsonify({"error": "bad_request", "detail": str(exc)}), 400


def _bad_opaque():
    """400 for a ``ValueError`` this extension did NOT raise (a uuid/int parse, SQLAlchemy, the JSON
    decoder). Its message describes internals, so it is logged and never echoed."""
    _log.warning("pastel: unexpected ValueError on %s", request.path, exc_info=True)
    return jsonify({"error": "bad_request", "detail": "invalid request"}), 400


def _not_found():
    # Deliberately identical for "no such paste" and "not yours" — see INV-TENANCY-01.
    return jsonify({"error": "not_found", "detail": "no such paste"}), 404


def _links(paste) -> dict:
    """The public GUID URLs for a paste, built from the browser blueprint so they track the mount."""
    return {
        "url": url_for("pastel.public_view", guid=paste.guid),
        "raw_url": url_for("pastel.public_raw", guid=paste.guid),
    }


@machine_bp.get("/pastes")
@host.require_scope("read")
def list_pastes():
    """List the pastes this token's user owns (or all of them for an admin)."""
    actor_id, _, is_admin = _principal()
    with get_config().session_factory() as db:
        rows = visible_pastes(db, author_id=actor_id, is_admin=is_admin)
        return jsonify(pastes=[to_dict(r, **_links(r)) for r in rows])


@machine_bp.post("/pastes")
@host.require_scope("write")
@request_body(CreatePasteRequest)
def create_paste():
    """Post a paste, attributed to the token's user, with optional expiry / read-limit / password."""
    actor_id, _username, _ = _principal()
    body = _body()
    with get_config().session_factory() as db:
        try:
            paste = create(
                db,
                author_id=actor_id,
                title=body.get("title"),
                body=body.get("body"),
                password=body.get("password"),
                expires_in=body.get("expires_in"),
                expires_at=body.get("expires_at"),
                max_reads=body.get("max_reads"),
            )
        except Denied as exc:
            return _denied(exc)
        except Invalid as exc:
            return _bad(exc)
        except ValueError:
            return _bad_opaque()
        return jsonify(paste=to_dict(paste, **_links(paste))), 201


@machine_bp.get("/pastes/<uuid:paste_id>")
@host.require_scope("read")
def get_paste(paste_id: uuid.UUID):
    """One paste's METADATA (not its body) — the token's own, or any for an admin. Else 404."""
    actor_id, _, is_admin = _principal()
    with get_config().session_factory() as db:
        paste = load_own(db, paste_id, author_id=actor_id, is_admin=is_admin)
        if paste is None:
            return _not_found()
        return jsonify(paste=to_dict(paste, **_links(paste)))


@machine_bp.delete("/pastes/<uuid:paste_id>")
@host.require_scope("write")
def delete_paste_api(paste_id: uuid.UUID):
    """Delete the token's own paste (or any, for an admin)."""
    actor_id, _, is_admin = _principal()
    with get_config().session_factory() as db:
        paste = load_own(db, paste_id, author_id=actor_id, is_admin=is_admin)
        if paste is None:
            return _not_found()
        try:
            delete_paste(db, paste, author_id=actor_id, is_admin=is_admin, host_audit=host_audit())
        except Denied as exc:
            return _denied(exc)
        return jsonify(status="deleted", id=str(paste_id))
