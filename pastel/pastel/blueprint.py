"""Human-facing UI — list, create, view, raw, delete.

Password-protected pastes use HTTP Basic auth: the server returns 401 + WWW-Authenticate and the
browser shows a native dialog. The username field is ignored; only the password matters.

A paste the caller may not see is 404, never 403 (INV-TENANCY-01, no existence oracle).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for

from pastel._version import __version__
from pastel.db import utcnow
from pastel.deps import (
    current_actor_id,
    current_actor_is_admin,
    current_actor_username,
    get_config,
    host_audit,
    host_can_write,
    is_standalone,
)
from pastel.service import (
    Denied,
    Gone,
    Invalid,
    NotFound,
    PasswordRequired,
    WrongPassword,
    access_and_count,
    list_own,
    load_by_slug,
)
from pastel.service import create as svc_create
from pastel.service import delete_paste as svc_delete

_log = logging.getLogger(__name__)

bp = Blueprint("pastel", __name__, template_folder="templates")

_TTL_MAP: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "pastel_base": cfg.base_template,
        "pastel_version": __version__,
        "pastel_can_write": host_can_write(),
        "pastel_is_admin": current_actor_is_admin(),
    }


def _require_write():
    if not host_can_write():
        abort(403)


def _basic_401() -> Response:
    """HTTP Basic auth challenge — wrong/missing password on a protected paste."""
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="Paste"'},
    )


def _extract_password() -> str | None:
    """Extract paste password from HTTP Basic auth (username ignored)."""
    auth = request.authorization
    if auth is None:
        return None
    return auth.password or None


def _parse_expires_at():
    ttl = (request.form.get("ttl") or "").strip()
    delta = _TTL_MAP.get(ttl)
    return (utcnow() + delta) if delta is not None else None


def _back(*, notice=None, error=None):
    return redirect(url_for("pastel.index", notice=notice, error=error))


@bp.get("/")
def index():
    actor_id = current_actor_id()
    standalone = is_standalone()
    notice = request.args.get("notice")
    error = request.args.get("error")
    with get_config().session_factory() as db:
        pastes = list_own(db, actor_id if not standalone else None)
    return render_template(
        "pastel/list.html", pastes=pastes, standalone=standalone, notice=notice, error=error
    )


@bp.post("/")
def create():
    _require_write()
    expires_at = _parse_expires_at()
    rl_str = (request.form.get("read_limit") or "").strip()
    read_limit = None
    if rl_str:
        try:
            read_limit = int(rl_str)
        except ValueError:
            return _back(error="invalid")
    password = request.form.get("password") or None
    with get_config().session_factory() as db:
        try:
            paste = svc_create(
                db,
                owner_id=current_actor_id(),
                owner_name=current_actor_username(),
                body=request.form.get("body"),
                title=request.form.get("title"),
                expires_at=expires_at,
                read_limit=read_limit,
                password=password,
                standalone=is_standalone(),
                audit_fn=host_audit(),
            )
        except Denied as exc:
            abort(403, str(exc))
        except Invalid:
            return _back(error="invalid")
        except ValueError:
            _log.warning("pastel: unexpected ValueError on %s", request.path, exc_info=True)
            return _back(error="invalid")
    return redirect(url_for("pastel.view_paste", slug=paste.slug))


def _serve(slug: str, raw: bool = False):
    """Shared logic for view/raw. Returns 401 Basic challenge for password-protected pastes."""
    password = _extract_password()
    with get_config().session_factory() as db:
        paste = load_by_slug(db, slug)
        if paste is None:
            abort(404)
        actor_id = current_actor_id()
        is_owner = paste.owner_id is not None and actor_id == paste.owner_id
        try:
            access_and_count(db, paste, password=password, is_owner=is_owner, audit_fn=host_audit())
        except (NotFound, Gone):
            abort(404)
        except (PasswordRequired, WrongPassword):
            return _basic_401()
    if raw:
        return Response(paste.body, content_type="text/plain; charset=utf-8")
    return render_template("pastel/view.html", paste=paste)


@bp.get("/p/<slug>")
def view_paste(slug: str):
    return _serve(slug, raw=False)


@bp.get("/p/<slug>/raw")
def raw_paste(slug: str):
    return _serve(slug, raw=True)


@bp.post("/delete/<slug>")
def delete_paste(slug: str):
    _require_write()
    with get_config().session_factory() as db:
        try:
            svc_delete(
                db, slug,
                owner_id=current_actor_id(),
                is_admin=current_actor_is_admin(),
                audit_fn=host_audit(),
            )
        except NotFound:
            abort(404)
        except Denied as exc:
            abort(403, str(exc))
    return _back(notice="deleted")
