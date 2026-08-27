"""Human-facing UI — one page: file a report, see your own, and (as an admin) respond to anyone's.

Plain HTML form POSTs, no JavaScript and no cookie-authed JSON API: the whole feature is a text box and
a list, and the platform already has forms. Every route resolves its authorization through
``bugreport.service`` — the same functions the PAT machine API uses.

A report the caller may not see is **404, never 403** (INV-TENANCY-01, no existence oracle).
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, abort, redirect, render_template, request, url_for

from bugreport._version import __version__
from bugreport.deps import (
    current_actor_id,
    current_actor_is_admin,
    current_actor_username,
    get_config,
    host_audit,
    host_blobs,
    host_can_write,
    is_standalone,
)
from bugreport.downloads import send_attachment
from bugreport.models import MAX_BODY, MAX_TITLE, ReportStatus
from bugreport.service import (
    Denied,
    Invalid,
    admin_act,
    attach,
    create,
    delete_attachment,
    delete_own,
    load_attachment_by_token,
    load_attachment_visible,
    load_visible,
    share_attachment,
    unshare_attachment,
    update_own,
)
from bugreport.service import visible_reports as _visible_reports

_log = logging.getLogger(__name__)

bp = Blueprint("bugreport", __name__, template_folder="templates")


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "bugreport_base": cfg.base_template,
        "bugreport_version": __version__,
        "bugreport_can_write": host_can_write(),
        "bugreport_is_admin": current_actor_is_admin(),
        "bugreport_statuses": [s.value for s in ReportStatus],
        "bugreport_max_title": MAX_TITLE,
        "bugreport_max_body": MAX_BODY,
    }


def _require_write():
    if not host_can_write():
        abort(403)


def _load_or_404(db, report_id: uuid.UUID):
    report = load_visible(
        db, report_id, actor_id=current_actor_id(), is_admin=current_actor_is_admin()
    )
    if report is None:
        abort(404)
    return report


@bp.get("/")
def index():
    """Your reports; plus, for an admin, everyone's."""
    actor_id, is_admin = current_actor_id(), current_actor_is_admin()
    standalone = is_standalone()
    with get_config().session_factory() as db:
        # An admin's OWN list is still their own rows — the "all" table is the extra, not a replacement.
        # Standalone is the exception: one local user owns everything, so there is nothing to split.
        mine = (
            _visible_reports(db, actor_id=actor_id, is_admin=standalone)
            if (actor_id is not None or standalone)
            else []
        )
        show_all = is_admin and not standalone
        every = _visible_reports(db, actor_id=actor_id, is_admin=True) if show_all else []
        return render_template("bugreport/list.html", mine=mine, every=every, standalone=standalone)


@bp.post("/")
def file_report():
    _require_write()
    with get_config().session_factory() as db:
        try:
            create(
                db,
                reporter_id=current_actor_id(),
                reporter_name=current_actor_username(),
                title=request.form.get("title"),
                body=request.form.get("body"),
                standalone=is_standalone(),
            )
        except Denied as exc:
            abort(403, str(exc))
        except Invalid as exc:
            abort(400, str(exc))
        except ValueError:
            # Not ours: a uuid/int parse, SQLAlchemy, the JSON decoder. The message describes
            # internals, so it is logged and never rendered (CodeQL: information exposure).
            _log.warning("bugreport: unexpected ValueError on %s", request.path, exc_info=True)
            abort(400, "invalid request")
    return redirect(url_for("bugreport.index"))


@bp.post("/<uuid:report_id>/update")
def update(report_id: uuid.UUID):
    """The reporter edits their own report's text."""
    _require_write()
    with get_config().session_factory() as db:
        report = _load_or_404(db, report_id)
        try:
            update_own(
                db,
                report,
                actor_id=current_actor_id(),
                title=request.form.get("title"),
                body=request.form.get("body"),
                standalone=is_standalone(),
            )
        except Denied as exc:
            abort(403, str(exc))
        except Invalid as exc:
            abort(400, str(exc))
        except ValueError:
            # Not ours: a uuid/int parse, SQLAlchemy, the JSON decoder. The message describes
            # internals, so it is logged and never rendered (CodeQL: information exposure).
            _log.warning("bugreport: unexpected ValueError on %s", request.path, exc_info=True)
            abort(400, "invalid request")
    return redirect(url_for("bugreport.index"))


@bp.post("/<uuid:report_id>/delete")
def delete(report_id: uuid.UUID):
    """The reporter deletes their own report (a real delete)."""
    _require_write()
    with get_config().session_factory() as db:
        report = _load_or_404(db, report_id)
        try:
            delete_own(db, report, actor_id=current_actor_id(), standalone=is_standalone())
        except Denied as exc:
            abort(403, str(exc))
    return redirect(url_for("bugreport.index"))


@bp.post("/<uuid:report_id>/respond")
def respond(report_id: uuid.UUID):
    """Admin: set the status and leave the note the reporter reads. ``status=deleted`` tombstones."""
    _require_write()
    with get_config().session_factory() as db:
        report = _load_or_404(db, report_id)
        try:
            admin_act(
                db,
                report,
                is_admin=current_actor_is_admin(),
                status=request.form.get("status"),
                note=request.form.get("note"),
                host_audit=host_audit(),
            )
        except Denied as exc:
            abort(403, str(exc))
        except Invalid as exc:
            abort(400, str(exc))
        except ValueError:
            # Not ours: a uuid/int parse, SQLAlchemy, the JSON decoder. The message describes
            # internals, so it is logged and never rendered (CodeQL: information exposure).
            _log.warning("bugreport: unexpected ValueError on %s", request.path, exc_info=True)
            abort(400, "invalid request")
    return redirect(url_for("bugreport.index"))


# --------------------------------------------------------------------------- attachments


def _blobs_or_503():
    blobs = host_blobs()
    if blobs is None:
        # Standalone, or a host with no object store. Say so rather than accepting an upload that
        # goes nowhere — a silent no-op here would look exactly like success.
        abort(503, "file storage is unavailable")
    return blobs


@bp.post("/<uuid:report_id>/attachments")
def upload_attachment(report_id: uuid.UUID):
    """Attach a file to a report the caller may see."""
    _require_write()
    blobs = _blobs_or_503()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        abort(400, "no file was supplied")
    with get_config().session_factory() as db:
        try:
            attach(
                db, blobs, report_id,
                actor_id=current_actor_id(), is_admin=current_actor_is_admin(),
                filename=upload.filename, claimed_type=upload.mimetype, stream=upload.stream,
            )
        except Denied as exc:
            abort(403, str(exc))
        except Invalid as exc:
            abort(400, str(exc))
        except ValueError:
            _log.warning("bugreport: unexpected ValueError on %s", request.path, exc_info=True)
            abort(400, "invalid request")
    return redirect(url_for("bugreport.index"))


@bp.get("/attachments/<uuid:attachment_id>/download")
def download_attachment(attachment_id: uuid.UUID):
    """The AUTHENTICATED path: identity decides, exactly like the report it hangs off."""
    blobs = _blobs_or_503()
    with get_config().session_factory() as db:
        row = load_attachment_visible(
            db, attachment_id, actor_id=current_actor_id(), is_admin=current_actor_is_admin()
        )
        if row is None:
            abort(404)
        try:
            return send_attachment(row, blobs)
        except KeyError:
            # The row survived its bytes. Not a 500: nothing is wrong with the request.
            _log.warning("bugreport: attachment %s has no stored bytes", attachment_id)
            abort(404)


@bp.get("/s/<token>")
def shared_attachment(token: str):
    """The ANONYMOUS path: the URL *is* the credential.

    Reachable without a session because the manifest declares ``[host] public_prefix = "/s"``, which
    core validated to be a strict sub-path of this extension's own mount. Everything about it is
    deliberately narrow:

    * it takes a FULL 256-bit token and returns one file, so there is nothing to enumerate;
    * there is no anonymous list, count, search or metadata surface anywhere in this extension, which
      is what keeps a no-engagement row inside INV-TENANCY-06's rule;
    * a wrong token is a flat 404 with no hint whether the file exists.
    """
    blobs = _blobs_or_503()
    with get_config().session_factory() as db:
        row = load_attachment_by_token(db, token)
        if row is None:
            abort(404)
        try:
            return send_attachment(row, blobs)
        except KeyError:
            abort(404)


@bp.post("/attachments/<uuid:attachment_id>/share")
def share(attachment_id: uuid.UUID):
    """Mint or ROTATE the share link. Rotating is how a leaked link is revoked."""
    _require_write()
    with get_config().session_factory() as db:
        try:
            share_attachment(
                db, attachment_id, actor_id=current_actor_id(), is_admin=current_actor_is_admin()
            )
        except Denied as exc:
            abort(403, str(exc))
    return redirect(url_for("bugreport.index"))


@bp.post("/attachments/<uuid:attachment_id>/unshare")
def unshare(attachment_id: uuid.UUID):
    _require_write()
    with get_config().session_factory() as db:
        try:
            unshare_attachment(
                db, attachment_id, actor_id=current_actor_id(), is_admin=current_actor_is_admin()
            )
        except Denied as exc:
            abort(403, str(exc))
    return redirect(url_for("bugreport.index"))


@bp.post("/attachments/<uuid:attachment_id>/delete")
def remove_attachment(attachment_id: uuid.UUID):
    _require_write()
    blobs = _blobs_or_503()
    with get_config().session_factory() as db:
        try:
            delete_attachment(
                db, blobs, attachment_id,
                actor_id=current_actor_id(), is_admin=current_actor_is_admin(),
            )
        except Denied as exc:
            abort(403, str(exc))
    return redirect(url_for("bugreport.index"))
