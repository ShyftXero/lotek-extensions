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

import logging
import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bugreport.models import (
    DEFAULT_CONTENT_TYPE,
    INLINE_SAFE_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_REPORT,
    MAX_BODY,
    MAX_TITLE,
    Attachment,
    Report,
    ReportStatus,
)

_log = logging.getLogger(__name__)

#: Newest-N ceiling on every list surface. A report body is up to MAX_BODY, filing is unrated-limited, and
#: an admin's list is unscoped — so without a cap one user can make the admin page (and `GET /reports`)
#: materialise an unbounded response. ponytail: fixed cap, add cursor pagination if anyone hits it.
LIST_LIMIT = 500

#: Bound the admin note copied into the core audit row — the note itself is free text, the audit is not
#: the place for 20k of it.
_AUDIT_NOTE_CHARS = 200


class Denied(PermissionError):
    """The caller is authenticated but not permitted (admin-only verb, or not the owner on a write)."""


class Invalid(ValueError):
    """Input this module rejected, carrying a message that is SAFE to show the caller.

    A distinct type, rather than a bare ``ValueError``, because both surfaces echo the message into a
    response body. ``except ValueError`` is a wide net: `uuid.UUID()`, `int()`, SQLAlchemy and the JSON
    decoder all raise it, and those messages describe internals, not the caller's mistake. CodeQL
    flagged exactly that (information exposure through an exception, api_pat.py:55/59). Only messages
    raised deliberately HERE are quotable; anything else gets a generic 400."""


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


def _text(value: object, field: str) -> str:
    """A JSON body is arbitrary — ``{"title": 123}`` and ``{"title": {"a": 1}}`` are both well-formed
    JSON, and the pydantic schemas on the machine routes only STAMP the model for the host's OpenAPI
    generator, they do not validate. Coercing with ``str()`` would silently store ``"{'a': 1}"``;
    calling ``.strip()`` on it raises AttributeError and the route 500s. Refuse it as a 400."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Invalid(f"{field} must be text")
    return value.strip()


def _clean(title: object, body: object) -> tuple[str, str]:
    title = _text(title, "title")
    body = _text(body, "body")
    if not title:
        raise Invalid("title is required")
    if len(title) > MAX_TITLE:
        raise Invalid(f"title is longer than {MAX_TITLE} characters")
    if len(body) > MAX_BODY:
        raise Invalid(f"body is longer than {MAX_BODY} characters")
    return title, body


def create(
    db: Session,
    *,
    reporter_id: uuid.UUID | None,
    reporter_name: str | None,
    title: object,
    body: object,
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
    db: Session, report: Report, *, actor_id: uuid.UUID | None, title: object, body: object,
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
    status: object,
    note: object,
    host_audit=None,
) -> Report:
    """The admin's whole write surface: set the status and leave a note the reporter reads.

    ``status='deleted'`` is a TOMBSTONE — the row stays so the reporter learns their report was deleted
    (#112). Audited through the host seam in the SAME transaction as the change (INV-AUDIT-03); an audit
    failure is not swallowed, it aborts the action."""
    if not is_admin:
        raise Denied("admin only")
    if note is not None:
        note = _text(note, "note")
        if len(note) > MAX_BODY:
            raise Invalid(f"note is longer than {MAX_BODY} characters")

    before = {"status": report.status.value, "admin_note": (report.admin_note or "")[:_AUDIT_NOTE_CHARS]}
    if status is not None:
        # `ReportStatus(...)` IS the validator: an unknown status raises ValueError, which every caller
        # already maps to 400. A second allow-list next to it would be a copy to keep in sync, and the
        # red-then-green pass proved it dead — deleting it changed no test. `_text` first because an
        # UNHASHABLE JSON value (a dict or a list) raises TypeError out of the enum lookup, not
        # ValueError, and a TypeError is a 500.
        report.status = ReportStatus(_text(status, "status"))
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


# --------------------------------------------------------------------------- attachments
#
# Visibility is INHERITED from the report, resolved by calling `load_visible` and adding nothing. There
# is deliberately no second ownership predicate for attachments: a copy is what drifts out of step, and
# this extension already keeps `db.get(Report, ...)` to exactly one call site for that reason.


def _sniff(head: bytes, claimed: object) -> str:
    """The content type we will SERVE this file as — never the uploader's claim.

    A claimed type is honoured only if it is in `INLINE_SAFE_TYPES` **and** the file's magic bytes
    agree. Everything else becomes `application/octet-stream` and is served as an attachment. This is
    what stops `evil.html` (or an SVG) arriving labelled `image/png` and then being rendered inline
    from lotek's own origin, where its JavaScript would run with the viewer's session.
    """
    want = str(claimed or "").split(";")[0].strip().lower()
    prefixes = INLINE_SAFE_TYPES.get(want)
    if not prefixes or not any(head.startswith(p) for p in prefixes):
        return DEFAULT_CONTENT_TYPE
    if want == "image/webp" and not (len(head) >= 12 and head[8:12] == b"WEBP"):
        # RIFF is a container fourcc, not a format.
        return DEFAULT_CONTENT_TYPE
    return want


class _CappedHeadReader:
    """Streams `fp`, enforcing `limit` as the bytes go past, after replaying an already-read head.

    The cap is applied to what is actually READ, never to `Content-Length` — that header is supplied by
    the same client supplying the body, so trusting it would make the limit advisory.
    """

    def __init__(self, head: bytes, fp, limit: int) -> None:
        self._head = head
        self._fp = fp
        self._limit = limit
        self._seen = len(head)

    def read(self, size: int = -1) -> bytes:
        if self._head:
            chunk, self._head = self._head, b""
            if size is not None and size >= 0 and len(chunk) > size:
                chunk, self._head = chunk[:size], chunk[size:]
            return chunk
        chunk = self._fp.read(size)
        self._seen += len(chunk)
        if self._seen > self._limit:
            raise Invalid(f"file is larger than {self._limit} bytes")
        return chunk


def _clean_filename(raw: object) -> str:
    """A display name for `Content-Disposition`. NEVER a path component.

    The stored object key is derived from this row's UUID primary key, so a hostile filename has
    nowhere to go — this only has to be safe to put in a header, so separators and control characters
    go and the length is bounded.
    """
    name = str(raw or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"\r\n')
    return name[:255] or "file"


def attach(
    db: Session, blobs, report_id: uuid.UUID, *, actor_id: uuid.UUID | None, is_admin: bool,
    filename: object, claimed_type: object, stream,
) -> Attachment:
    """Store one uploaded file against a report the caller may see."""
    report = load_visible(db, report_id, actor_id=actor_id, is_admin=is_admin)
    if report is None:
        raise Denied("no such report")
    if report.status is ReportStatus.deleted:
        raise Denied("this report was deleted by an admin and can no longer be changed")
    existing = db.scalar(
        select(func.count()).select_from(Attachment).where(Attachment.report_id == report.id)
    )
    if (existing or 0) >= MAX_ATTACHMENTS_PER_REPORT:
        raise Invalid(f"a report may carry at most {MAX_ATTACHMENTS_PER_REPORT} attachments")

    head = stream.read(16) or b""
    serve_as = _sniff(head, claimed_type)
    row = Attachment(
        report_id=report.id,
        uploader_id=actor_id,
        filename=_clean_filename(filename),
        content_type=serve_as,
    )
    db.add(row)
    db.flush()  # assign the PK; the object key is derived from it
    ref = blobs.put(row.id, _CappedHeadReader(head, stream, MAX_ATTACHMENT_BYTES), content_type=serve_as)
    row.size = ref.size
    row.sha256 = ref.sha256
    try:
        db.commit()
    except Exception:
        # The bytes are already in the store but the row is not. Core writes no `objects` row for
        # extension blobs, so nothing in core can ever find an orphan like this — the only party that
        # knows the key is us, right here. Clean it up before the id is lost.
        _log.warning("bugreport: attachment row failed to commit; removing its blob", exc_info=True)
        try:
            blobs.delete(row.id)
        except Exception:  # noqa: BLE001 — best effort; the original failure is what matters
            _log.warning("bugreport: could not remove orphaned blob %s", row.id, exc_info=True)
        raise
    return row


def attachments_for(db: Session, report: Report) -> list[Attachment]:
    """Every attachment on a report the caller has ALREADY been cleared to see."""
    return list(
        db.scalars(
            select(Attachment)
            .where(Attachment.report_id == report.id)
            .order_by(Attachment.created_at.asc())
            .limit(MAX_ATTACHMENTS_PER_REPORT)
        )
    )


def load_attachment_visible(
    db: Session, attachment_id: uuid.UUID, *, actor_id: uuid.UUID | None, is_admin: bool
) -> Attachment | None:
    """One attachment the caller may READ, or ``None`` -> rendered as 404 either way (no oracle)."""
    row = db.get(Attachment, attachment_id)
    if row is None:
        return None
    if load_visible(db, row.report_id, actor_id=actor_id, is_admin=is_admin) is None:
        return None
    return row


def load_attachment_by_token(db: Session, token: object) -> Attachment | None:
    """The ANONYMOUS path: exactly one attachment, addressed by its bearer capability.

    Strictly single-object on purpose. INV-TENANCY-06 permits an extension row with no engagement id
    only behind an explicit declared platform rule, and forbids a list/count/queue surface disclosing
    rows outside the caller's scope. A share link is neither a list nor a query — it is a 256-bit
    capability naming one row — so this takes a full token and returns one row or nothing. There is no
    anonymous listing, counting, searching or enumeration anywhere in this extension.

    A tombstoned report's attachments stop resolving: an admin who removes a report should not leave
    its evidence reachable by an old link.
    """
    tok = str(token or "")
    if len(tok) < 32:  # a real token is 43 chars; refuse obvious probes without a query
        return None
    row = db.scalar(select(Attachment).where(Attachment.share_token == tok))
    if row is None:
        return None
    report = db.get(Report, row.report_id)
    if report is None or report.status is ReportStatus.deleted:
        return None
    return row


def _attachment_for_write(
    db: Session, attachment_id: uuid.UUID, *, actor_id: uuid.UUID | None, is_admin: bool,
    admin_may_act: bool = True,
) -> Attachment:
    """One attachment the caller may WRITE, or ``Denied``.

    ``admin_may_act=False`` narrows the verb to the OWNER ONLY, even for an admin. That distinction is
    the declared policy, not a nicety: an admin may **revoke** anyone's public link (a link that leaked
    is an incident, and waiting for the owner is the wrong failure mode) but may **not mint one on
    someone else's file**. Publishing another person's upload is not a moderation action, and an admin
    who needs it can say so out loud rather than having the code do it silently.
    """
    row = load_attachment_visible(db, attachment_id, actor_id=actor_id, is_admin=is_admin)
    if row is None:
        raise Denied("no such attachment")
    report = db.get(Report, row.report_id)
    # Defence in depth: `load_attachment_visible` already decided, and an attachment whose report
    # vanished between the two reads is refused rather than treated as ownerless.
    if report is None:
        raise Denied("no such attachment")
    if _owns(report, actor_id):
        return row
    if is_admin and admin_may_act:
        return row
    raise Denied("only the reporter may share their own attachments")


def share_attachment(
    db: Session, attachment_id: uuid.UUID, *, actor_id: uuid.UUID | None, is_admin: bool,
    host_audit=None,
) -> str:
    """Mint (or ROTATE) the bearer capability for one attachment and return it.

    Rotating is the revocation story for a link that leaked: every previously-issued URL stops working
    the moment this is called again.

    AUDITED, and that is not optional decoration: this is the one verb in the extension that hands an
    UNAUTHENTICATED stranger a way to read a file. An outward capability grant with no trail is exactly
    what INV-AUDIT-03's vocabulary exists to make readable afterwards. The token itself is NEVER written
    to the audit row — a durable log is the last place a live credential should sit (INV-SECRET-04);
    the row records THAT sharing happened, not the secret.
    """
    # admin_may_act=False: minting is the OWNER's verb. See _attachment_for_write.
    row = _attachment_for_write(
        db, attachment_id, actor_id=actor_id, is_admin=is_admin, admin_may_act=False
    )
    was_shared = row.share_token is not None
    token = secrets.token_urlsafe(32)
    row.share_token = token
    if host_audit is not None:
        host_audit(
            db,
            "ext:bugreport:share_file",
            subject_type="bugreport_attachment",
            subject_id=row.id,
            before={"shared": was_shared},
            # "rotated" distinguishes revoking-a-leak from first publication, which is the question a
            # human reads this row to answer.
            after={"shared": True, "rotated": was_shared},
        )
    db.commit()
    return token


def unshare_attachment(
    db: Session, attachment_id: uuid.UUID, *, actor_id: uuid.UUID | None, is_admin: bool,
    host_audit=None,
) -> bool:
    """Revoke the capability. Idempotent, and audited for the same reason minting is."""
    row = _attachment_for_write(db, attachment_id, actor_id=actor_id, is_admin=is_admin)
    was_shared = row.share_token is not None
    row.share_token = None
    if host_audit is not None:
        host_audit(
            db,
            "ext:bugreport:unshare_file",
            subject_type="bugreport_attachment",
            subject_id=row.id,
            before={"shared": was_shared},
            after={"shared": False},
        )
    db.commit()
    return True


def delete_attachment(
    db: Session, blobs, attachment_id: uuid.UUID, *, actor_id: uuid.UUID | None, is_admin: bool
) -> bool:
    """Remove the row and its bytes."""
    row = _attachment_for_write(db, attachment_id, actor_id=actor_id, is_admin=is_admin)
    blob_id = row.id
    db.delete(row)
    db.commit()
    try:
        blobs.delete(blob_id)
    except Exception:  # noqa: BLE001 — the row is gone; an orphaned blob is a cleanup task, not a 500
        _log.warning("bugreport: attachment %s row deleted but its blob was not", blob_id, exc_info=True)
    return True


def attachment_to_dict(row: Attachment) -> dict:
    """Metadata only. The share token is included because only someone already cleared to see the
    attachment ever reaches this, and it is what they need in order to share it."""
    return {
        "id": str(row.id),
        "report_id": str(row.report_id),
        "filename": row.filename,
        "content_type": row.content_type,
        "size": row.size,
        "sha256": row.sha256,
        "shared": row.share_token is not None,
        "share_token": row.share_token,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def claimed_blob_ids(db: Session, ids: set) -> set:
    """Which of ``ids`` this extension still has an ``Attachment`` row for.

    The host calls this from its leader-only reclamation sweep (`[host] blob_claims` in the manifest ->
    `object_store.reconcile_extension_blobs`) and deletes the bytes for anything we do NOT return.
    Extension blobs carry no core `objects` row, so without this there is no path that can ever reclaim
    a blob whose row never landed — a killed process between `put` and `commit` leaks storage forever.

    Two deliberate properties, because the caller turns the answer into deletions:

    * It answers ONLY about the ids it was asked about. It never volunteers a broader set, so a bug here
      cannot widen what the host considers claimed.
    * It queries rows, not the store. If the row exists, the blob is claimed — no heuristics, no
      "probably still needed". The host applies its own age floor before it ever asks.
    """
    if not ids:
        return set()
    return set(db.scalars(select(Attachment.id).where(Attachment.id.in_(ids))))
