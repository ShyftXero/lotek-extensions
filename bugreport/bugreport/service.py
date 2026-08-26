"""Bugreport domain logic — the visibility rule, the write rules, validation, and the admin audit.

Kept free of Flask so it is unit-testable, and so **both** surfaces (the browser blueprint and the PAT
machine API) route through the SAME functions. INV-TENANCY-01's phrasing is the point: the decision comes
from one shared predicate rather than per-handler checks. A second copy of this rule is how it inverts.

The declared platform rule (INV-TENANCY-06, for a row that legitimately holds no engagement id):

    A report is visible to its reporter and to an admin. Nobody else, on any surface.

A caller who is not permitted gets **404, not 403** (INV-TENANCY-01: responses must be indistinguishable
for "not authorized" and "does not exist", or the surface is an existence oracle). ``load_visible``
returns ``None`` for both cases so a caller cannot accidentally tell them apart.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from bugreport.models import MAX_BODY, MAX_TITLE, Report, ReportStatus

#: Newest-N ceiling on every list surface. A report body is up to MAX_BODY, filing is unrated-limited, and
#: an admin's list is unscoped — so without a cap one user can make the admin page (and `GET /reports`)
#: materialise an unbounded response. ponytail: fixed cap, add cursor pagination if anyone hits it.
LIST_LIMIT = 500

#: Bound the admin note copied into the core audit row — the note itself is free text, the audit is not
#: the place for 20k of it.
_AUDIT_NOTE_CHARS = 200


class Denied(PermissionError):
    """The caller is authenticated but not permitted (admin-only verb, or not the owner on a write)."""


def _owns(report: Report, actor_id: uuid.UUID | None, standalone: bool = False) -> bool:
    # `actor_id is None` short-circuits BEFORE the comparison: an anonymous MOUNTED caller must never match
    # a row whose reporter_id is also NULL. Standalone is the single-local-user case and is the ONLY thing
    # that makes a NULL-owner row ownable — which is why it is an explicit argument, not a None check.
    if standalone:
        return True
    return actor_id is not None and report.reporter_id == actor_id


def visible_reports(db: Session, *, actor_id: uuid.UUID | None, is_admin: bool) -> list[Report]:
    """The newest :data:`LIST_LIMIT` reports the caller may READ. Admin -> all; otherwise the caller's own
    only. An anonymous caller (``actor_id is None``, not admin) sees nothing — the filter is applied in
    SQL, so no out-of-scope row is ever loaded into the process."""
    stmt = select(Report).order_by(Report.created_at.desc()).limit(LIST_LIMIT)
    if not is_admin:
        if actor_id is None:
            return []
        stmt = stmt.where(Report.reporter_id == actor_id)
    return list(db.scalars(stmt).all())


def load_visible(
    db: Session, report_id: uuid.UUID, *, actor_id: uuid.UUID | None, is_admin: bool
) -> Report | None:
    """One report the caller may READ, or ``None`` — which the caller renders as **404** whether the row
    is missing or merely out of scope (no existence oracle)."""
    report = db.get(Report, report_id)
    if report is None:
        return None
    if is_admin or _owns(report, actor_id):
        return report
    return None


def _clean(title: str | None, body: str | None) -> tuple[str, str]:
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        raise ValueError("title is required")
    if len(title) > MAX_TITLE:
        raise ValueError(f"title is longer than {MAX_TITLE} characters")
    if len(body) > MAX_BODY:
        raise ValueError(f"body is longer than {MAX_BODY} characters")
    return title, body


def create(
    db: Session,
    *,
    reporter_id: uuid.UUID | None,
    reporter_name: str | None,
    title: str | None,
    body: str | None,
    standalone: bool = False,
) -> Report:
    """File a report, attributed to ``reporter_id``.

    Mounted, an unresolvable reporter is REFUSED rather than written unattributable — an ownerless row
    would be invisible to every non-admin forever, and unattributable in the admin's queue."""
    if reporter_id is None and not standalone:
        raise Denied("a report must be attributable to a signed-in user")
    title, body = _clean(title, body)
    report = Report(
        reporter_id=reporter_id,
        reporter_name=(reporter_name or None),
        title=title,
        body=body,
        status=ReportStatus.open,
    )
    db.add(report)
    db.commit()
    return report


def update_own(
    db: Session, report: Report, *, actor_id: uuid.UUID | None, title: str | None, body: str | None,
    standalone: bool = False,
) -> Report:
    """The reporter edits their OWN report's text. Admin is deliberately NOT allowed here: an admin
    silently rewriting somebody else's words is not "admin CRUD", it is a forgery surface. An admin
    responds through :func:`admin_act`."""
    if not _owns(report, actor_id, standalone):
        raise Denied("only the reporter may edit a report")
    if report.status is ReportStatus.deleted:
        raise Denied("this report was deleted by an admin and can no longer be edited")
    report.title, report.body = _clean(title, body)
    db.commit()
    return report


def delete_own(
    db: Session, report: Report, *, actor_id: uuid.UUID | None, standalone: bool = False
) -> None:
    """The reporter deletes their OWN report — a real row delete. There is nobody left to give feedback
    to, which is exactly why an ADMIN delete is a tombstone instead (see :func:`admin_act`)."""
    if not _owns(report, actor_id, standalone):
        raise Denied("only the reporter may delete their own report")
    db.delete(report)
    db.commit()


def admin_act(
    db: Session,
    report: Report,
    *,
    is_admin: bool,
    status: str | None,
    note: str | None,
    host_audit=None,
) -> Report:
    """The admin's whole write surface: set the status and leave a note the reporter reads.

    ``status='deleted'`` is a TOMBSTONE — the row stays so the reporter learns their report was deleted
    (#112). Audited through the host seam in the SAME transaction as the change (INV-AUDIT-03); an audit
    failure is not swallowed, it aborts the action."""
    if not is_admin:
        raise Denied("admin only")
    if note is not None:
        note = note.strip()
        if len(note) > MAX_BODY:
            raise ValueError(f"note is longer than {MAX_BODY} characters")

    before = {"status": report.status.value, "admin_note": (report.admin_note or "")[:_AUDIT_NOTE_CHARS]}
    if status is not None:
        # `ReportStatus(...)` IS the validator: an unknown status raises ValueError, which every caller
        # already maps to 400. A second allow-list next to it would be a copy to keep in sync, and the
        # red-then-green pass proved it dead — deleting it changed no test.
        report.status = ReportStatus(status)
    # `note is None` means "the caller did not send one" -> KEEP the existing note. Only an explicitly
    # empty string clears it. Blanking it on a status-only PATCH would silently destroy the reporter's
    # feedback, which is the one thing this extension exists to deliver.
    if note is not None:
        report.admin_note = note or None
    after = {"status": report.status.value, "admin_note": (report.admin_note or "")[:_AUDIT_NOTE_CHARS]}

    if host_audit is not None:
        # Actor + source are resolved host-side FROM THE REQUEST, never passed in (INV-AUDIT-03).
        host_audit(
            db,
            "ext:bugreport:admin_update",
            subject_type="bugreport_report",
            subject_id=report.id,
            before=before,
            after=after,
        )
    db.commit()
    return report


def to_dict(report: Report) -> dict:
    """JSON projection. Text only — there is nothing else to project."""
    return {
        "id": str(report.id),
        "reporter_id": str(report.reporter_id) if report.reporter_id else None,
        "reporter_name": report.reporter_name,
        "title": report.title,
        "body": report.body,
        "status": report.status.value,
        "admin_note": report.admin_note,
        "created_at": report.created_at.isoformat(),
        "updated_at": report.updated_at.isoformat(),
    }
