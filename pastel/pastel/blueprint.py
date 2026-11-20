"""Human-facing UI — a markdown editor, the author's own paste list, an authed owner preview, and the
PUBLIC GUID view + ``/raw``.

Two surfaces, deliberately split by how a read is counted:

* **Owner surface** (``/``, ``/<paste_id>...``) is session-authed and keys on ``paste_id`` (the internal
  UUID PK). The owner previewing their own paste does NOT count as a read (#369: key on the unguessable
  GUID fetch, not an authed owner view).
* **Public surface** (``/p/<guid>`` + ``/p/<guid>/raw``) is UNAUTHENTICATED — the GUID *is* the
  credential — and every fetch that returns content runs the full access gate and counts as a read.

A paste the caller may not see is **404, never 403** (INV-TENANCY-01, no existence oracle). An expired,
over-limit, retired, or unknown GUID is an identical flat 404.
"""

from __future__ import annotations

import logging
import uuid

from flask import (
    Blueprint,
    Response,
    abort,
    redirect,
    render_template,
    request,
    url_for,
)

from pastel._version import __version__
from pastel.deps import (
    current_actor_id,
    current_actor_is_admin,
    get_config,
    host_audit,
    host_can_write,
    is_standalone,
)
from pastel.models import MAX_BODY, MAX_TITLE, TTL_TOKENS
from pastel.render_html import render_markdown
from pastel.service import (
    Denied,
    Invalid,
    PasswordRequired,
    adjust_access,
    create,
    delete_paste,
    fetch_for_read,
    load_own,
    update_own,
    visible_pastes,
)

_log = logging.getLogger(__name__)

bp = Blueprint("pastel", __name__, template_folder="templates")


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "pastel_base": cfg.base_template,
        "pastel_version": __version__,
        "pastel_can_write": host_can_write(),
        "pastel_max_title": MAX_TITLE,
        "pastel_max_body": MAX_BODY,
        "pastel_ttl_tokens": list(TTL_TOKENS.keys()),
    }


def _require_write():
    if not host_can_write():
        abort(403)


def _back(*, notice: str | None = None, error: str | None = None):
    """PRG back to the list. ``notice``/``error`` are short CODES, not free text: the template maps a
    known code to a fixed message and renders nothing for an unknown one, so a crafted ``?error=<x>``
    link cannot reflect attacker text into the banner."""
    return redirect(url_for("pastel.index", notice=notice, error=error))


# --------------------------------------------------------------------------- owner surface


@bp.get("/")
def index():
    """The editor plus the caller's own pastes (every paste, for an admin)."""
    actor_id, is_admin = current_actor_id(), current_actor_is_admin()
    with get_config().session_factory() as db:
        mine = visible_pastes(db, author_id=actor_id, is_admin=is_admin)
        return render_template("pastel/list.html", pastes=mine, standalone=is_standalone())


@bp.post("/")
def create_paste():
    _require_write()
    with get_config().session_factory() as db:
        try:
            paste = create(
                db,
                author_id=current_actor_id(),
                title=request.form.get("title"),
                body=request.form.get("body"),
                password=request.form.get("password") or None,
                expires_in=request.form.get("expires_in"),
                max_reads=request.form.get("max_reads"),
                host_audit=host_audit(),
            )
        except Invalid:
            return _back(error="invalid")
        except ValueError:
            _log.warning("pastel: unexpected ValueError on %s", request.path, exc_info=True)
            return _back(error="invalid")
        paste_id = paste.id
    return redirect(url_for("pastel.owner_view", paste_id=paste_id, notice="created"))


def _load_own_or_404(db, paste_id: uuid.UUID):
    paste = load_own(
        db, paste_id, author_id=current_actor_id(), is_admin=current_actor_is_admin()
    )
    if paste is None:
        abort(404)
    return paste


@bp.get("/<uuid:paste_id>")
def owner_view(paste_id: uuid.UUID):
    """The author's own preview — rendered, with the shareable GUID link. Does NOT count as a read."""
    with get_config().session_factory() as db:
        paste = _load_own_or_404(db, paste_id)
        ctx = {
            "paste": paste,
            "body_html": render_markdown(paste.body),
            "share_url": url_for("pastel.public_view", guid=paste.guid),
            "raw_url": url_for("pastel.public_raw", guid=paste.guid),
            "owner": True,
        }
        return render_template("pastel/view.html", **ctx)


@bp.post("/<uuid:paste_id>/update")
def update(paste_id: uuid.UUID):
    """The author edits their paste's text (title/body). Access-control settings are create-time only."""
    _require_write()
    with get_config().session_factory() as db:
        paste = _load_own_or_404(db, paste_id)
        try:
            update_own(
                db, paste,
                author_id=current_actor_id(), is_admin=current_actor_is_admin(),
                title=request.form.get("title"), body=request.form.get("body"),
            )
        except Denied as exc:
            abort(403, str(exc))
        except Invalid:
            return _back(error="invalid")
        except ValueError:
            _log.warning("pastel: unexpected ValueError on %s", request.path, exc_info=True)
            return _back(error="invalid")
    return redirect(url_for("pastel.owner_view", paste_id=paste_id, notice="updated"))


@bp.post("/<uuid:paste_id>/access")
def adjust_paste_access(paste_id: uuid.UUID):
    """Owner adjusts access controls: add reads, extend expiry (days from now), set/clear password."""
    _require_write()

    def _pos_int(name):
        raw = (request.form.get(name) or "").strip()
        if not raw:
            return None
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    with get_config().session_factory() as db:
        paste = _load_own_or_404(db, paste_id)
        try:
            adjust_access(
                db, paste,
                author_id=current_actor_id(), is_admin=current_actor_is_admin(),
                add_reads=_pos_int("add_reads"), extend_days=_pos_int("extend_days"),
                new_password=((request.form.get("new_password") or "").strip() or None),
                clear_password=bool(request.form.get("clear_password")),
                host_audit=host_audit(),
            )
        except Denied as exc:
            abort(403, str(exc))
        except Invalid:
            return _back(error="invalid")
    return redirect(url_for("pastel.owner_view", paste_id=paste_id, notice="updated"))


@bp.post("/<uuid:paste_id>/delete")
def delete(paste_id: uuid.UUID):
    _require_write()
    with get_config().session_factory() as db:
        paste = _load_own_or_404(db, paste_id)
        try:
            delete_paste(
                db, paste,
                author_id=current_actor_id(), is_admin=current_actor_is_admin(),
                host_audit=host_audit(),
            )
        except Denied as exc:
            abort(403, str(exc))
    return _back(notice="deleted")


@bp.post("/bulk-delete")
def bulk_delete():
    """Delete one or more of the caller's own pastes selected on the list page. Each id is gated the
    same as single delete (load_own + delete_paste); unknown / not-owned / bad ids are skipped, so a
    stale checkbox can never 403 the whole batch."""
    _require_write()
    author_id, is_admin = current_actor_id(), current_actor_is_admin()
    with get_config().session_factory() as db:
        for raw in request.form.getlist("ids"):
            try:
                pid = uuid.UUID(raw)
            except (ValueError, TypeError, AttributeError):
                continue
            paste = load_own(db, pid, author_id=author_id, is_admin=is_admin)
            if paste is None:
                continue
            try:
                delete_paste(
                    db, paste,
                    author_id=author_id, is_admin=is_admin, host_audit=host_audit(),
                )
            except Denied:
                continue
    return _back(notice="deleted")


# --------------------------------------------------------------------------- public surface (GUID)


def _challenge() -> Response:
    """401 with an HTTP Basic challenge — the only non-404 a password-protected paste gives an
    unauthenticated fetcher, so a legitimate reader's browser can prompt for the password."""
    return Response(
        "password required\n",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="pastel", charset="UTF-8"'},
        mimetype="text/plain",
    )


def _basic_password() -> str | None:
    """The password from an HTTP Basic header, if any. The username is ignored — a paste password is a
    single secret, not a username/password pair."""
    auth = request.authorization
    if auth is not None and (auth.type or "").lower() == "basic":
        return auth.password
    return None


@bp.get("/p/<guid>")
def public_view(guid: str):
    """Rendered public view. Runs the full access gate and COUNTS as a read."""
    with get_config().session_factory() as db:
        try:
            paste = fetch_for_read(db, guid, password=_basic_password(), host_audit=host_audit())
        except PasswordRequired:
            return _challenge()
        if paste is None:
            abort(404)
        ctx = {
            "paste": paste,
            "body_html": render_markdown(paste.body),
            "raw_url": url_for("pastel.public_raw", guid=guid),
            "owner": False,
        }
        return render_template("pastel/view.html", **ctx)


@bp.get("/p/<guid>/raw")
def public_raw(guid: str):
    """Raw markdown source as ``text/plain``. Runs the full access gate and COUNTS as a read (a ``/raw``
    fetch that returns content is a read, same as the rendered view — #369)."""
    with get_config().session_factory() as db:
        try:
            paste = fetch_for_read(db, guid, password=_basic_password(), host_audit=host_audit())
        except PasswordRequired:
            return _challenge()
        if paste is None:
            abort(404)
        return Response(
            paste.body, mimetype="text/plain; charset=utf-8",
            # raw attacker-authored bytes: never let a sniffing client render them as HTML.
            headers={"X-Content-Type-Options": "nosniff"},
        )
