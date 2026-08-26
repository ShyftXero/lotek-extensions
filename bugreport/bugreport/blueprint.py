"""Human-facing UI — one page: file a report, see your own, and (as an admin) respond to anyone's.

Plain HTML form POSTs, no JavaScript and no cookie-authed JSON API: the whole feature is a text box and
a list, and the platform already has forms. Every route resolves its authorization through
``bugreport.service`` — the same functions the PAT machine API uses.

A report the caller may not see is **404, never 403** (INV-TENANCY-01, no existence oracle).
"""

from __future__ import annotations

import uuid

from flask import Blueprint, abort, redirect, render_template, request, url_for

from bugreport._version import __version__
from bugreport.deps import (
    current_actor_id,
    current_actor_is_admin,
    current_actor_username,
    get_config,
    host_audit,
    host_can_write,
    is_standalone,
)
from bugreport.models import MAX_BODY, MAX_TITLE, ReportStatus
from bugreport.service import Denied, admin_act, create, delete_own, load_visible, update_own
from bugreport.service import visible_reports as _visible_reports

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
        except ValueError as exc:
            abort(400, str(exc))
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
        except ValueError as exc:
            abort(400, str(exc))
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
        except ValueError as exc:
            abort(400, str(exc))
    return redirect(url_for("bugreport.index"))
