"""PAT-scoped MACHINE API — mounted at ``<url_prefix>/machine`` on its OWN blueprint.

This is how an AGENT files a bug report (#112: "a generic text-only way for **agents** and users"). Bearer
token + scope RBAC, the same contract lotek's ``/api/v1`` and the sibling extensions' machine APIs use;
distinct from the cookie-authed browser page at ``<url_prefix>/``.

TENANCY: a machine request has no session, so ``bugreport.deps.current_actor_*`` are all None here.
Identity comes from the PAT principal (``host.actor()``) and is fed into the SAME
``bugreport.service`` functions the browser surface uses — the visibility rule has exactly one
implementation, so it cannot drift between the two surfaces. A report the token may not see is
**404, never 403** (INV-TENANCY-01).
"""

from __future__ import annotations

import uuid

from flask import Blueprint, jsonify, request

from bugreport import host
from bugreport.api_schemas import CreateReportRequest, UpdateReportRequest, request_body
from bugreport.deps import get_config, host_audit
from bugreport.service import (
    Denied,
    admin_act,
    create,
    delete_own,
    load_visible,
    to_dict,
    update_own,
    visible_reports,
)

machine_bp = Blueprint("bugreport_machine", __name__)
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


def _bad(exc: Exception):
    return jsonify({"error": "bad_request", "detail": str(exc)}), 400


def _not_found():
    # Deliberately identical for "no such report" and "not yours" — see INV-TENANCY-01.
    return jsonify({"error": "not_found", "detail": "no such report"}), 404


@machine_bp.get("/reports")
@host.require_scope("read")
def list_reports():
    """List the bug reports this token's user may see — their own, or all of them for an admin."""
    actor_id, _, is_admin = _principal()
    with get_config().session_factory() as db:
        rows = visible_reports(db, actor_id=actor_id, is_admin=is_admin)
        return jsonify(reports=[to_dict(r) for r in rows])


@machine_bp.post("/reports")
@host.require_scope("write")
@request_body(CreateReportRequest)
def create_report():
    """File a bug report, attributed to the token's user. Text only."""
    actor_id, username, _ = _principal()
    body = _body()
    with get_config().session_factory() as db:
        try:
            # `standalone=False`: a machine call only exists because a host authenticated the token, so
            # an unresolvable principal is a refusal, never an unattributable row.
            report = create(
                db,
                reporter_id=actor_id,
                reporter_name=username,
                title=body.get("title"),
                body=body.get("body"),
                standalone=False,
            )
        except Denied as exc:
            return _denied(exc)
        except ValueError as exc:
            return _bad(exc)
        return jsonify(report=to_dict(report)), 201


@machine_bp.get("/reports/<uuid:report_id>")
@host.require_scope("read")
def get_report(report_id: uuid.UUID):
    """One bug report — the token's own, or any of them for an admin. Otherwise 404."""
    actor_id, _, is_admin = _principal()
    with get_config().session_factory() as db:
        report = load_visible(db, report_id, actor_id=actor_id, is_admin=is_admin)
        if report is None:
            return _not_found()
        return jsonify(report=to_dict(report))


@machine_bp.patch("/reports/<uuid:report_id>")
@host.require_scope("write")
@request_body(UpdateReportRequest)
def update_report(report_id: uuid.UUID):
    """Reporter edit (``title``/``body``) OR admin response (``status``/``note``) — never both at once."""
    actor_id, _, is_admin = _principal()
    body = _body()
    owner_fields = {k: body[k] for k in ("title", "body") if k in body}
    admin_fields = {k: body[k] for k in ("status", "note") if k in body}
    if owner_fields and admin_fields:
        return jsonify({
            "error": "bad_request",
            "detail": "send a reporter edit (title/body) or an admin response (status/note), not both",
        }), 400
    if not owner_fields and not admin_fields:
        return jsonify({"error": "bad_request", "detail": "nothing to update"}), 400

    with get_config().session_factory() as db:
        report = load_visible(db, report_id, actor_id=actor_id, is_admin=is_admin)
        if report is None:
            return _not_found()
        try:
            if admin_fields:
                admin_act(
                    db, report, is_admin=is_admin, status=admin_fields.get("status"),
                    note=admin_fields.get("note"), host_audit=host_audit(),
                )
            else:
                # Fall back to the stored value so a partial edit does not blank the other field.
                update_own(
                    db, report, actor_id=actor_id,
                    title=owner_fields.get("title", report.title),
                    body=owner_fields.get("body", report.body),
                )
        except Denied as exc:
            return _denied(exc)
        except ValueError as exc:
            return _bad(exc)
        return jsonify(report=to_dict(report))


@machine_bp.delete("/reports/<uuid:report_id>")
@host.require_scope("write")
def delete_report(report_id: uuid.UUID):
    """The reporter deletes their OWN report (a real delete).

    An ADMIN deletes by tombstoning instead — ``PATCH {"status": "deleted", "note": "..."}`` — because a
    removed row cannot tell its reporter it was deleted, which is what #112 asks for. So this route is
    owner-only even for an admin acting on somebody else's report.
    """
    actor_id, _, is_admin = _principal()
    with get_config().session_factory() as db:
        report = load_visible(db, report_id, actor_id=actor_id, is_admin=is_admin)
        if report is None:
            return _not_found()
        try:
            delete_own(db, report, actor_id=actor_id)
        except Denied as exc:
            return _denied(exc)
        return jsonify(status="deleted", id=str(report_id))
