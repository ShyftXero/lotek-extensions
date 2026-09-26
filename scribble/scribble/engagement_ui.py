"""ReportBoard + finding-board UI (WS3 owns this module).

The keystone workstream: engagement CRUD, `AssessmentType`-linked `FindingGroup` management, "add
finding from template", the finding detail page (mounting the WS4 editor + WS5 gallery + WS6 preview),
and the two-level drag-and-drop board itself. Board order (``FindingGroup.order_index`` then, within a
group, ``order_mode``-dependent finding order) is the same ordering `reporting.build_report_context`
reads — see PLAN.md §4 "Grouping & ordering UX" and §9.

Contract: expose `def register(api_bp, bp) -> None` (idempotent) adding routes to `bp` (UI) + `api_bp`
(reorder/move JSON). Keep endpoint names `engagements` and `engagement_board`. Already wired into
`scribble/__init__.py:_wire_feature_routes` (frozen file, not touched here).

Routes added
------------
UI (``bp``, mounted at the host ``url_prefix``, default ``/scribble``):
    GET  /engagements                                   list (endpoint: engagements)
    GET  /engagements/new                                new-engagement form
    POST /engagements/new                                create (select-or-create Client)
    GET  /engagements/<id>/edit                          edit form (endpoint: engagement_edit_page)
    POST /engagements/<id>/edit                          apply edits (endpoint: engagement_edit) --
                                                          shares field-parsing with create via
                                                          ``_apply_engagement_form``, but (unlike create)
                                                          also lets ``status`` be changed post-creation.
    POST /engagements/<id>/delete                        delete the engagement and everything under it
                                                          (endpoint: engagement_delete) -- groups/findings/
                                                          artifacts/variable_values cascade via the
                                                          ``delete-orphan`` relationships already declared
                                                          on ``ReportBoard`` in models.py.
    GET  /engagements/<id>                               the board (endpoint: engagement_board)
    POST /engagements/<id>/groups                        create a FindingGroup
    POST /engagements/<id>/groups/<group_id>/delete       delete a group (findings -> ungrouped, not lost)
    POST /engagements/<id>/findings                       add a finding from a VulnerabilityTemplate
    POST /engagements/<id>/findings/<finding_id>/delete   delete a finding AND its artifacts (endpoint:
                                                           delete_finding) -- unlike group delete, a
                                                           finding IS its content, so its evidence goes
                                                           with it; its nested per-host CHILDREN do not,
                                                           they are detached (findings_service)
    GET  /findings/<id>                                   finding detail (endpoint: finding_detail)
    POST /findings/<id>                                   update finding meta

API (``api_bp``, mounted at ``<url_prefix>/api``), all JSON:
    POST /engagements/<id>/groups/reorder   {"order": [group_id, ...]}   -> persists group order_index
    POST /findings/<id>/move                {"group_id": int|null, "order_index": int}
                                             -> cross-group reassign + within-group reorder; the target
                                             group flips to OrderMode.manual (see PLAN.md §4).
    POST /groups/<id>                       {"name"?, "order_mode"?, "include_in_report"?}
                                             -> rename / toggle / "re-rank by severity"
                                             (order_mode="auto_severity" resets a manually-ordered group).

All mutating handlers are written defensively: stale/foreign/duplicate ids in a reorder payload are
ignored (not a 500); moving into a nonexistent or cross-engagement group is a 400/404, never a silent
corruption; deleting a group detaches its findings (group_id -> NULL) instead of cascading their
deletion, since a report *section* is not the same thing as the findings inside it -- conversely,
deleting a finding (``delete_finding``) DOES cascade to its own artifacts, since a finding is its
content and evidence, not a container for other authored rows -- but its nested per-host CHILDREN are
DETACHED like a group's findings are, because those rows carry evidence of their own (and, until that was
handled, deleting a promoted parent violated the ``parent_id`` self-FK and 500'd); 404 (never 500) if the
finding doesn't exist or belongs to a different engagement, mirroring ``delete_group``'s guard.

``ReportBoard.created_by`` / ``BoardFinding.created_by`` are set from the optional host-injected
``current_actor`` hook (``scribble.deps.current_actor_username``) -- ``None`` standalone.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import date

from flask import abort, jsonify, redirect, render_template, request, send_file, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from scribble import finding_grouping_adapter, findings_service, host
from scribble.artifacts_storage import delete_file, guess_content_type
from scribble.authz import can_view_client_id, can_view_engagement, host_is_mounted
from scribble.content import schema
from scribble.deps import (
    client_model,
    current_actor,
    current_actor_id,
    current_actor_username,
    host_can_write,
    open_session,
    severity_enum,
)
from scribble.enums import Confidence, FindingStatus, OrderMode, RetestOutcome
from scribble.models import (
    AssessmentType,
    BoardFinding,
    EngagementDiagram,
    FindingGroup,
    ReportBoard,
    ScribbleReportLogo,
    ScribbleSectionPreset,
    VulnerabilityTemplate,
    normalize_strategic_recommendations,
)
from scribble.templating import known_variable_keys

_REGISTERED = False

# Cover-logo library upload limits. Raster images only — an SVG can carry script and the cover serves the
# image inline, so restricting to raster types avoids a stored-XSS surface AND keeps the logo embeddable in
# the .docx (InlineImage needs a raster). Small brand images, so a tight ceiling.
_MAX_LOGO_BYTES = 5 * 1024 * 1024
_LOGO_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
# Cap the text sent to the host AI hook for a rephrase — a standing-prose section, not a whole report.
_MAX_REPHRASE_CHARS = 20_000


# --------------------------------------------------------------------------------- small helpers


from scribble.artifacts_api import _as_uuid  # noqa: E402  -- one shared body-id parser (lotek#335)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_id(value) -> int | uuid.UUID | None:
    """Parse a form-submitted ``client_id`` tolerantly: EITHER host id shape ``ReportBoard.client_id``
    (``scribble.models.SoftHostId``) can hold -- a plain int (standalone Scribble / legacy hosts) or a
    UUID (Lotek v2's UUIDv7 client PKs). Unlike ``_as_int``, a UUID string is not silently dropped: it
    parses to a real ``uuid.UUID`` (never a bare string -- see SoftHostId's docstring for why a raw
    string crashes a lookup against a UUID-typed host PK). Anything that is neither -> ``None``, same
    fail-safe posture as ``_as_int`` (an unparseable/empty value must not 500, just fall through to the
    new-client-by-name branch or leave the engagement unlinked)."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _viewable_clients(db) -> list:
    """The clients this actor may attach an engagement to, for the create/edit form's ``<select>``.

    Both forms rendered the mounted host's ENTIRE client table -- every client's name and id, to anyone
    with a Scribble login. That is the engagement-list leak one table over: the client roster IS tenancy
    data. Standalone (``scribble_clients``, no host) is unfiltered, there being nothing to filter by.
    """
    ClientModel = client_model()
    rows = list(db.scalars(select(ClientModel).order_by(ClientModel.name)).all())
    if not host_is_mounted():
        return rows
    actor = current_actor()
    return [c for c in rows if can_view_client_id(c.id, actor)]


def _resolve_client(db, form):
    """Resolve the client an engagement is created under / moved to -> ``(client_id, error_message)``.

    The client field IS the engagement's tenancy, and it arrives in the request body, so no id-shaped gate
    can reach it: unchecked, this creates an engagement under someone else's client, or (edit) MOVES an
    engagement you legitimately hold onto a client you don't -- planting readable data in another tenant,
    or pushing your own out of your reach.

    Three rules, all only when a host is mounted (standalone keeps the original behaviour verbatim):

    * a named ``client_id`` must be one the actor may view -> otherwise ``abort(404)``, matching the rest
      of the module's no-existence-oracle posture rather than a 403 that confirms the id is real;
    * ``new_client_name`` is refused. ``client_model()`` resolves to the HOST's own client table when
      mounted, so this form was creating rows in lotek's tenancy data from an extension form, under no
      membership of the creator's -- the resulting engagement would be unopenable by the person who just
      made it. Clients are the host's to create;
    * the client is REQUIRED, because ``can_view_client(None, actor)`` is False by the host's contract: a
      client-less engagement 404s for everyone, so creating one is a success response for nothing.

    Mounted, the granted id is returned WITHOUT looking the row up: ``ReportBoard.client_id`` is a soft
    reference (docs/LOTEK_ADOPTION.md §3.1) and the host has just been asked the only question that
    matters about it. Requiring a resolvable row here would add a second, weaker source of truth about
    which clients are real. The standalone branch keeps its original select-existing-or-create-by-name
    behaviour verbatim, rows and all — there is no host to own the client table there.
    """
    ClientModel = client_model()
    client_id = _as_id(form.get("client_id"))
    new_client_name = (form.get("new_client_name") or "").strip()

    if host_is_mounted():
        if client_id is not None:
            if not can_view_client_id(client_id, current_actor()):
                abort(404)
            return client_id, None
        if new_client_name:
            return None, (
                "Clients are managed by the host — pick an existing one. (A client created here would "
                "land in the host's client table with no membership of yours, and the engagement would "
                "open for nobody.)"
            )
        return None, "A client is required — an engagement with no client can be opened by nobody."

    client = db.get(ClientModel, client_id) if client_id is not None else None
    if client is None and new_client_name:
        client = db.scalar(select(ClientModel).where(ClientModel.name == new_client_name))
        if client is None:
            client = ClientModel(name=new_client_name)
            db.add(client)
            db.flush()
    return (client.id if client is not None else None), None


def _apply_engagement_form(engagement: ReportBoard, form, db) -> str | None:
    """Shared field-setting for the edit route (port of lotek's ``routes/engagements.py::
    _apply_engagement_form``). Unlike ``engagement_new`` above -- which never touches ``status`` and
    leaves it at the model default ("in_progress") -- this ALSO sets ``status``, since edit is the first
    place a user can change it after creation.

    Client resolution mirrors ``engagement_new``'s select-existing-or-create-by-name convention (rather
    than lotek's simpler client_id-only parse), so editing behaves the same way creating does -- INCLUDING
    the tenancy rules in :func:`_resolve_client`, whose refusal message this returns (``None`` = applied).
    The client is resolved FIRST so a refusal leaves the row untouched rather than half-updated.
    """
    client_id, error = _resolve_client(db, form)
    if error is not None:
        return error

    # lotek#620: validate the manual overall-risk override BEFORE mutating the row (like the client
    # above), so a bad override leaves the engagement untouched. Empty select = no override; a set
    # override needs a non-empty rationale (the same rule the machine PATCH enforces).
    raw_override = (form.get("risk_override") or "").strip()
    rationale = (form.get("risk_override_rationale") or "").strip()
    override_value = None
    if raw_override:
        try:
            override_value = severity_enum()(raw_override.lower())
        except ValueError:
            return f"Invalid risk override: {raw_override}"
        if not rationale:
            return "A manual risk override needs a rationale (say why you adjusted it)."

    engagement.name = (form.get("name") or "").strip()
    engagement.scope_type = (form.get("scope_type") or "external").strip() or "external"
    engagement.company_name = (form.get("company_name") or "").strip() or None
    engagement.status = (form.get("status") or "in_progress").strip() or "in_progress"
    engagement.start_date = _parse_date(form.get("start_date"))
    engagement.end_date = _parse_date(form.get("end_date"))
    engagement.client_id = client_id
    # Clearing the override clears its reason — no dangling rationale.
    engagement.risk_override = override_value
    engagement.risk_override_rationale = rationale if override_value else None
    # lotek#623: one recommendation per line in the textarea; the shared normalizer drops blank lines and
    # trims, so an empty textarea clears the list.
    engagement.strategic_recommendations = normalize_strategic_recommendations(
        (form.get("strategic_recommendations") or "").splitlines()
    )
    # lotek#642: KEV/EPSS enrichment egresses each CVE to public feeds, so it stays OFF until an operator
    # opts in per engagement. This checkbox is the ONLY writer of the flag the enrichment driver reads
    # (enrichment.egress_consented); an unchecked box clears prior consent.
    engagement.threat_intel_egress_consent = "threat_intel_egress_consent" in form
    return None


def _adopt_job_onto_board(db, engagement: ReportBoard, job_id: str, actor) -> None:
    """Link + pour ONE scan job onto ``engagement`` — the SINGLE shared body of ``adopt_job`` and the
    by-core one-click ``adopt_job_by_core``, so every promote routes through ONE place and never forks
    the #845 predicate.

    Order is load-bearing (mirrors the machine route): the anchor check (#845) runs FIRST, then the
    refuse-on-conflict mark (#632) GATES the pour — a cross-engagement job 409s and a job already
    adopted elsewhere 409s, and NOTHING is poured or linked in either case. An unknown/not-viewable job
    (host ``get_job`` -> ``None``) is a silent no-op — the same one-answer-no-leak posture the routes give.

    The CALLER owns write-gating (``host_can_write``) and resolving/committing ``engagement``; this body
    is only get_job -> anchor -> mark -> promote. ``abort(409)`` raises through the view.
    """
    findings_ns = host.findings()
    job = findings_ns.get_job(job_id, actor) if (job_id and findings_ns is not None) else None
    if job is None:  # unknown/not-viewable/no id -> silent no-op, exactly like the routes
        return
    from scribble.promote import (  # lazy: promote.py is Track D's file
        CrossEngagementPromote,
        assert_promote_anchor,
        promote_job,
    )
    # Anchor check (#845) BEFORE the gating mark, so a cross-engagement adopt never leaves the job
    # linked-but-not-poured. Same single predicate the machine route uses.
    try:
        assert_promote_anchor(engagement, getattr(job, "engagement_id", None))
    except CrossEngagementPromote as exc:
        abort(409, f"This scan job belongs to engagement {exc.job_engagement_id}, "
                   f"but this report board is anchored to engagement {exc.anchor}. "
                   f"Reassign the job first.")
    # Link FIRST so it can gate: refuse-on-conflict returns False -> 409, pour nothing.
    if not host.mark_job_promoted(job_id, actor, extension="scribble", ref_id=engagement.id):
        abort(409, "This scan job is already adopted by another engagement.")
    dtos = findings_ns.list_findings(job_id, actor)
    promote_job(db, engagement=engagement, findings=dtos,
                actor_username=current_actor_username(),
                job_engagement_id=getattr(job, "engagement_id", None))


def _board_for_core(db, core_id: uuid.UUID) -> ReportBoard:
    """Resolve the report board LINKED to ``core_id``, CREATING it (name + client DERIVED from the scoped
    summary, never trusted from input) if none exists — the SINGLE board-create seam, shared by
    ``import_board`` and the one-click ``adopt_job_by_core`` so a board is never an orphan with no core
    engagement behind it. The CALLER owns the ``can_operate_on`` gate and the commit. ``abort(404)`` if
    the core id isn't a summary the actor can see (the create path needs its name/client)."""
    existing = db.execute(
        select(ReportBoard)
        .where(ReportBoard.core_engagement_id == core_id)
        .order_by(ReportBoard.id)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    summ = next((s for s in host.engagement_summaries() if s["id"] == core_id), None)
    if summ is None:
        abort(404)
    board = ReportBoard(
        name=summ["name"],
        client_id=summ["client_id"],
        core_engagement_id=core_id,
        created_by=current_actor_username(),
        owner_id=current_actor_id(),
    )
    db.add(board)
    db.flush()  # populate board.id for the redirect / adopt ref_id before the caller commits
    return board


def register(api_bp, bp) -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # =============================================================================== UI: engagements

    @bp.get("/engagements", endpoint="engagements")
    def engagements():
        """Report Boards: a VIEW over the viewer's CORE engagements, not a parallel engagement list.

        Each row is a core engagement (from the host seam, scoped to what the caller may see). It either
        already has a report board (Open it) or offers Import-to-Scribble (create a board LINKED to that
        core engagement). One "engagement" concept in the product; Scribble is its reporting layer. A
        board is 1:1 with a core engagement (partial-unique on core_engagement_id), so at most one per row.
        """
        summaries = host.engagement_summaries()  # [{id, name, client_id, client_name}], already scoped
        core_ids = [s["id"] for s in summaries]
        with open_session() as db:
            boards = {}
            if core_ids:
                for b in db.execute(
                    select(ReportBoard).where(ReportBoard.core_engagement_id.in_(core_ids))
                ).scalars().all():
                    if b.core_engagement_id is not None:
                        boards[b.core_engagement_id] = b
            rows = [
                {
                    "core_id": s["id"],
                    "name": s["name"],
                    "client_name": s["client_name"],
                    "board": boards.get(s["id"]),
                }
                for s in summaries
            ]
        return render_template("scribble/engagements.html", rows=rows)

    @bp.post("/engagements/import", endpoint="import_board")
    def import_board():
        """One-click Import-to-Scribble: create a report board LINKED to a core engagement (or open the
        existing one). The ONLY create path — a board never exists without a core engagement behind it.

        Tenancy: gated on ``host.can_operate_on`` (operator on that engagement) and every failure — bad
        id, not-operator, unknown — collapsed to one 404, so this leaks nothing. Name + client are
        DERIVED from the core engagement (via the same scoped summaries), never trusted from the form,
        so the form carries only the core id. Idempotent: a second import opens the existing board.
        """
        raw = (request.form.get("core_engagement_id") or "").strip()
        try:
            core_id = uuid.UUID(raw)
        except (ValueError, AttributeError):
            abort(404)
        if not host.can_operate_on(core_id):
            abort(404)
        with open_session() as db:
            board = _board_for_core(db, core_id)  # resolve-or-create, the shared seam
            db.commit()
            return redirect(url_for("scribble.engagement_board", engagement_id=board.id))

    @bp.route("/engagements/new", methods=["GET", "POST"], endpoint="engagement_new")
    def engagement_new():
        # Standalone board creation is retired (Report Board rework): a report board is created ONLY by
        # importing a core engagement (see ``import_board``), so it can never be an orphan with no core
        # engagement behind it. Kept as a redirect so a stale link/bookmark lands on the Report Boards
        # list rather than 404ing.
        return redirect(url_for("scribble.engagements"))

    @bp.get("/engagements/by-core/<core_id>", endpoint="engagement_by_core")
    def engagement_by_core(core_id):
        """Reverse of the board's source-jobs panel (#629): a core engagement page links HERE to reach
        its report board. Resolve the board by ``ReportBoard.core_engagement_id``; if none exists yet,
        send the operator to the create form pre-seeded to LINK to this core engagement (not spawn a
        second one).

        Tenancy: gate on the host's own ``can_operate_on`` and collapse every failure to one 404, so
        this leaks nothing about a core engagement the caller can't operate -- unknown id, malformed
        id, and "exists but not yours" are indistinguishable (same rule as ``_resolve_engagement``).
        There is no UNIQUE constraint on ``core_engagement_id`` (models.py: index, not unique), so a
        collision resolves deterministically to the oldest board rather than raising.
        """
        try:
            key = uuid.UUID(str(core_id))
        except (ValueError, AttributeError):
            abort(404)
        if not host.can_operate_on(key):
            abort(404)
        with open_session() as db:
            row = db.execute(
                select(ReportBoard)
                .where(ReportBoard.core_engagement_id == key)
                .order_by(ReportBoard.id)
                .limit(1)
            ).scalar_one_or_none()
        if row is not None:
            return redirect(url_for("scribble.engagement_board", engagement_id=row.id))
        return redirect(url_for("scribble.engagements"))

    @bp.post("/engagements/by-core/<core_id>/adopt-job/<job_id>", endpoint="adopt_job_by_core")
    def adopt_job_by_core(core_id, job_id):
        """One-click "Add to report" (#847): the POST twin of ``engagement_by_core``. A core job page
        POSTs HERE (CSRF-protected, so unlike a GET link it can't be triggered cross-site) to pour a
        job's findings onto the report board anchored to its core engagement in ONE click.

        Resolves-or-CREATES the board for the core engagement (the shared ``_board_for_core`` seam — the
        SAME summary-derived create as ``import_board``, so a board is never an orphan), then adopts the
        job via ``_adopt_job_onto_board`` (the SAME #845 anchor + #632 refuse-on-conflict as
        ``adopt_job`` — a cross-engagement / already-adopted job 409s identically, and the just-created
        board rolls back with it since the commit is last). WRITE-gated + tenancy-gated
        (``can_operate_on``, one 404 on failure)."""
        if not host_can_write():
            abort(403)
        try:
            key = uuid.UUID(str(core_id))
        except (ValueError, AttributeError):
            abort(404)
        if not host.can_operate_on(key):
            abort(404)
        job_id = (job_id or "").strip()
        with open_session() as db:
            board = _board_for_core(db, key)
            # An unknown/unviewable job makes the adopt a silent no-op (no existence oracle): the board is
            # still created (exactly as a bare import would) and we still redirect to it, so a real-vs-bogus
            # job is indistinguishable in the response. A 409 (cross-engagement/already-adopted) instead
            # rolls the fresh board back — commit is last.
            _adopt_job_onto_board(db, board, job_id, current_actor())
            db.commit()
            return redirect(url_for("scribble.engagement_board", engagement_id=board.id))

    # =============================================================================== UI: edit / delete

    @bp.get("/engagements/<uuid:engagement_id>/edit", endpoint="engagement_edit_page")
    def engagement_edit_page(engagement_id: int):
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            return render_template(
                "scribble/engagement_edit.html",
                engagement=engagement,
                clients=_viewable_clients(db),
                error=None,
            )

    @bp.post("/engagements/<uuid:engagement_id>/edit", endpoint="engagement_edit")
    def engagement_edit(engagement_id: int):
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            name = (request.form.get("name") or "").strip()
            if not name:
                return (
                    render_template(
                        "scribble/engagement_edit.html",
                        engagement=engagement,
                        clients=_viewable_clients(db),
                        error="Name is required.",
                    ),
                    400,
                )
            # A refusal here means the form named a client this actor may not move the engagement to
            # (or none at all, while mounted) -- nothing is committed, so the row is untouched.
            error = _apply_engagement_form(engagement, request.form, db)
            if error is not None:
                return (
                    render_template(
                        "scribble/engagement_edit.html",
                        engagement=engagement,
                        clients=_viewable_clients(db),
                        error=error,
                    ),
                    400,
                )
            db.commit()
        return redirect(url_for("scribble.engagements"))

    @bp.post("/engagements/<uuid:engagement_id>/delete", endpoint="engagement_delete")
    def engagement_delete(engagement_id: int):
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            # Mirrors delete_finding's explicit artifact-file cleanup below: the ORM cascade
            # (ReportBoard.artifacts, cascade="all, delete-orphan") removes the Artifact ROWS, but the
            # bytes on disk are not the ORM's to clean up -- collect the paths before the cascade delete,
            # then best-effort remove the files afterward, same as delete_finding does per-finding.
            storage_paths = [a.storage_path for a in engagement.artifacts]
            # Clear everything that references a FINDING of this engagement from OUTSIDE the cascade
            # graph, FIRST: the delete-orphan cascade below emits its finding DELETEs in one unordered
            # batch, so a surviving reference makes that batch violate an FK and the engagement cannot be
            # deleted AT ALL. That is the self-FK parent_id link (a promoted aggregation), and equally a
            # CollabDoc (written by the co-editing room the moment a human opens a block), a
            # finding-scoped VariableValue, or a checklist item's finding_id. See
            # findings_service.prepare_engagement_delete, which owns the whole set.
            findings_service.prepare_engagement_delete(db, engagement)
            db.delete(engagement)  # cascades to groups/findings/artifacts/variable_values (delete-orphan)
            db.commit()
        for storage_path in storage_paths:
            delete_file(storage_path)
        return redirect(url_for("scribble.engagements"))

    # =============================================================================== UI: board (detail)

    @bp.get("/engagements/<uuid:engagement_id>", endpoint="engagement_board")
    def engagement_board(engagement_id: int):
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)

            groups = sorted(engagement.groups, key=lambda g: g.order_index)
            board_groups = [
                {"group": g, "findings": findings_service.display_order(g.findings, g.order_mode)}
                for g in groups
            ]
            ungrouped = findings_service.ungrouped_display_order(engagement)

            templates = db.scalars(
                select(VulnerabilityTemplate)
                .where(VulnerabilityTemplate.active.is_(True))
                .order_by(VulnerabilityTemplate.name)
            ).all()
            assessment_types = db.scalars(
                select(AssessmentType)
                .where(AssessmentType.active.is_(True))
                .order_by(AssessmentType.default_order, AssessmentType.name)
            ).all()

            client = engagement.resolve_client(db)

            # ReportBoard-level evidence (ext#51): artifacts attached to the engagement itself, not to
            # any finding (``finding_id`` null). These render into the client report's Evidence
            # appendix (ext#40 / ``reporting/context.py``'s ``ReportContext.artifacts``) but previously
            # had no UI review/exclude surface -- an operator could only discover what published by
            # reading the rendered report.
            engagement_artifacts = sorted(
                (a for a in engagement.artifacts if a.finding_id is None),
                key=lambda a: (a.order_index, a.id),
            )

            diagrams = sorted(engagement.diagrams, key=lambda d: (d.order_index, d.id))

            # Reverse of promotion (#629): the host scan jobs whose findings were promoted INTO this
            # engagement. The host seam (`host.list_jobs`) is the ONE home for this derived view and
            # applies its own `user_can_view_job` to the session actor; [] standalone / unmounted.
            source_jobs = host.list_jobs(engagement, current_actor())

            # By-vulnerability / by-host rollups (Phase 1b, lotek #829): a re-pivot of the SAME board
            # findings through core's shared bucketer via the host seam, so an adopted job with a
            # fleet-wide vuln (EternalBlue on every host, self-signed certs, LLMNR) collapses to one row
            # per kind instead of flooding the board. Flatten promotion shells to their per-host children
            # first. Empty off-mount -> the template hides the extra tabs (assessment-group view stands).
            group_rows = [
                finding_grouping_adapter.board_finding_to_group_row(f)
                for f in findings_service.flatten_for_grouping(engagement.findings)
            ]
            findings_by_kind = host.group_findings(group_rows, by="kind")
            findings_by_host = host.group_findings(group_rows, by="host")

            # Report Layout composer (kit section-composer): the resolved per-report section order + on/off
            # for the drag-to-reorder widget, and the shipped presets as one-click "apply this order" seeds.
            from scribble.reporting.layouts import list_layouts, resolve_section_order
            section_specs = resolve_section_order(engagement.section_order)
            # Preset combobox options: built-in layouts + org-wide saved presets (#Q10). Each carries its
            # full [{key,enabled}] specs as JSON so selecting one applies both order AND visibility. A
            # built-in lists its blocks enabled; the widget disables everything else.
            saved_presets = db.scalars(
                select(ScribbleSectionPreset).order_by(ScribbleSectionPreset.name)
            ).all()
            composer_presets = [
                {"label": lay.label, "builtin": True, "id": "",
                 "specs_json": json.dumps([{"key": k, "enabled": True} for k in lay.blocks])}
                for lay in list_layouts()
            ] + [
                {"label": p.name, "builtin": False, "id": str(p.id), "specs_json": json.dumps(p.specs)}
                for p in saved_presets
            ]
            # Cover-logo library (org-wide) + this report's current pick, for the cover-logo picker.
            cover_logos = db.scalars(
                select(ScribbleReportLogo).order_by(ScribbleReportLogo.created_at.desc())
            ).all()
            current_logo_id = engagement.cover_logo_id
            # Rephrase-with-AI is offered only when the host provides an AI hook (off by default).
            ai_available = host.host_hook("ai_complete") is not None

            return render_template(
                "scribble/engagement.html",
                engagement=engagement,
                client_name=(client.name if client is not None else None),
                board_groups=board_groups,
                ungrouped=ungrouped,
                findings_by_kind=findings_by_kind,
                findings_by_host=findings_by_host,
                templates=templates,
                assessment_types=assessment_types,
                engagement_artifacts=engagement_artifacts,
                diagrams=diagrams,
                source_jobs=source_jobs,
                section_specs=section_specs,
                composer_presets=composer_presets,
                cover_logos=cover_logos,
                current_logo_id=current_logo_id,
                ai_available=ai_available,
            )

    # =============================================================================== UI: groups

    @bp.post("/engagements/<uuid:engagement_id>/groups", endpoint="create_group")
    def create_group(engagement_id: int):
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            name = (request.form.get("name") or "").strip()
            if name:
                assessment_type_id = _as_uuid(request.form.get("assessment_type_id"))
                at = db.get(AssessmentType, assessment_type_id) if assessment_type_id is not None else None
                findings_service.create_group(db, engagement, name=name, assessment_type=at)
                db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    @bp.post("/engagements/<uuid:engagement_id>/groups/<uuid:group_id>/delete", endpoint="delete_group")
    def delete_group(engagement_id: int, group_id: int):
        with open_session() as db:
            group = db.get(FindingGroup, group_id)
            if group is None or group.engagement_id != engagement_id:
                abort(404)
            # Detaches its child findings rather than deleting them — see findings_service.delete_group.
            findings_service.delete_group(db, group)
            db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    # =============================================================================== UI: add finding

    @bp.post("/engagements/<uuid:engagement_id>/findings", endpoint="add_finding")
    def add_finding(engagement_id: int):
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)

            template_id = _as_uuid(request.form.get("template_id"))
            template = db.get(VulnerabilityTemplate, template_id) if template_id is not None else None
            if template is not None:
                group_id = _as_uuid(request.form.get("group_id"))
                group = db.get(FindingGroup, group_id) if group_id is not None else None
                if group is not None and group.engagement_id != engagement_id:
                    group = None  # defensive: never attach to another engagement's group

                siblings = (
                    group.findings
                    if group is not None
                    else [f for f in engagement.findings if f.group_id is None]
                )  # count only — a new finding goes last, so display order is irrelevant here
                finding = BoardFinding.from_template(
                    template,
                    engagement_id=engagement_id,
                    group_id=group.id if group is not None else None,
                    order_index=len(siblings),
                    created_by=current_actor_username(),
                )
                db.add(finding)
                db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    # ============================================================================ UI: promote scan job

    @bp.post("/engagements/<uuid:engagement_id>/promote-job", endpoint="promote_job")
    def promote_job_ui(engagement_id: uuid.UUID):
        """Human twin of the machine route (`api_pat.scribble_promote_job`): pull a lotek scan job's
        findings onto THIS board from the browser.

        Before this route, promotion existed ONLY on the PAT/machine surface — a person operating in the
        browser could not turn a finished scan into a report at all, which is why a UI-only lifecycle had
        to hand-build findings from templates. Found by driving the app as a human (BusyBody, 2026-08-27).

        Tenancy and CSRF are handled the same way every other human route here handles them, so this body
        stays as thin as `add_finding`: the blueprint-wide `_gate` has already resolved + authorized
        `engagement_id` before we run (unknown/forbidden -> 404, no existence leak), the host applies
        `user_can_view_job` to the session actor inside `get_job`/`list_findings` (unknown/forbidden job
        -> None -> a no-op, same one-answer-no-leak the machine twin gives), and the host owns CSRF
        (lotek does NOT exempt `/scribble/*`). Aggregation is `scribble.promote.promote_job`'s concern.

        `job_id` arrives as a FORM field, not a URL segment, because there is no host hook to LIST an
        engagement's promotable jobs yet, so the operator supplies the id (BusyBody carries it from the
        queue_job step). A job `<select>` is the follow-on once a host `list_jobs` hook exists.
        """
        # Route-level WRITE gate. The blueprint `_gate` only authorizes VIEW, and every other scribble
        # mutating route (add_finding, create_group, delete_*) currently relies on that plus the UI-only
        # `scribble_can_write` display flag — so a viewer who can SEE an engagement can POST a mutation
        # directly. That is a SYSTEMIC scribble gap (noted for a broader fix); a NEW mutating route must
        # not ship with the weaker posture, so promotion — which pours a whole scan's findings onto the
        # board — checks write at the route. `host_can_write()` defaults True when no host hook is wired
        # (standalone), and reads the host's real capability under lotek.
        if not host_can_write():
            abort(403)
        job_id = (request.form.get("job_id") or "").strip()
        actor = current_actor()
        promoted_ref = None
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            findings_ns = host.findings()
            job = findings_ns.get_job(job_id, actor) if (job_id and findings_ns is not None) else None
            if job is not None:
                from scribble.promote import CrossEngagementPromote, promote_job  # lazy: promote.py
                dtos = findings_ns.list_findings(job_id, actor)
                try:
                    promote_job(db, engagement=engagement, findings=dtos,
                                actor_username=current_actor_username(),
                                job_engagement_id=getattr(job, "engagement_id", None))
                except CrossEngagementPromote as exc:
                    # Operator-visible refusal (#845), never a silent no-op: this board is anchored to a
                    # different core engagement than the job's. 409 with how to fix it.
                    abort(409, f"This report board is anchored to engagement {exc.anchor}; the scan job "
                               f"belongs to engagement {exc.job_engagement_id}. Reassign the job, or "
                               f"promote it into that engagement's board.")
                db.commit()
                promoted_ref = engagement.id  # capture inside the session for the host-side write below
        if promoted_ref is not None:
            # The one host-contract write, in its own transaction (mirrors the machine route).
            host.mark_job_promoted(job_id, actor, extension="scribble", ref_id=promoted_ref)
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    @bp.post("/engagements/<uuid:engagement_id>/adopt-job/<job_id>", endpoint="adopt_job")
    def adopt_job(engagement_id: uuid.UUID, job_id: str):
        """Adopt a scan job into THIS engagement — the Source-jobs picker (#630) posts here with the
        chosen job id in the path. Adopting both LINKS the job (`host.mark_job_promoted`) and pours its
        findings onto the board (`promote_job`), the two halves of `promote_job_ui` above.

        The one behaviour that route lacks and this one adds: the link is REFUSE-ON-CONFLICT (core #632).
        `promote_job_ui` marks best-effort and ignores the result, so a job already promoted into another
        engagement is silently re-poured; here the mark is the GATE — a refusal (`False`) surfaces as 409
        and NOTHING is poured, so a conflicting job is never re-pointed and never double-linked. Same
        tenancy posture as its twin: WRITE-gated at the route, and an unknown/forbidden job (host
        `get_job` -> None) is a silent no-op redirect — not-found and not-viewable are indistinguishable,
        no existence leak. The get_job -> anchor -> mark -> pour body is the shared ``_adopt_job_onto_board``
        (also driven by the by-core one-click ``adopt_job_by_core``), so both share ONE #845 predicate.
        """
        if not host_can_write():
            abort(403)
        actor = current_actor()
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            _adopt_job_onto_board(db, engagement, job_id, actor)
            db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    @bp.get("/engagements/<uuid:engagement_id>/adoptable-jobs.json", endpoint="adoptable_jobs_json")
    def adoptable_jobs_json(engagement_id: uuid.UUID):
        """Searchable source for the Source-jobs adopt picker (#234): the scan jobs the caller may adopt
        for THIS board's core engagement's client, matching ``?q=``. Returns the combobox contract
        (``{"items":[{value,label}]}``) so ``combobox.js`` in ``data-search-url`` mode drives it — an
        operator picks a job by name/target/status/date instead of pasting an opaque UUID.

        Engagement-scoped by the blueprint gate (a non-member 404s before this runs); the host
        ``adoptable_jobs`` hook scopes to the caller's visible jobs on top (``scope_jobs_query``). No
        core engagement behind the board (standalone) -> the hook returns nothing.
        """
        with open_session() as db:
            board = db.get(ReportBoard, engagement_id)
            if board is None:
                abort(404)
            core_id = board.core_engagement_id
        q = (request.args.get("q") or "").strip()
        items = []
        for j in host.adoptable_jobs(core_id, q, 50):
            name = j.get("name") or "(unnamed)"
            tgt = ((j.get("targets") or "").splitlines() or [""])[0][:40]
            label = f"{name} — {tgt} · {j.get('status', '')} · {(j.get('created_at') or '')[:10]}"
            items.append({"value": j["id"], "label": label})
        return jsonify(items=items)

    # =============================================================================== UI: un-adopt (#635)

    def _is_source_job(engagement, job_id, actor) -> bool:
        """Is ``job_id`` actually adopted into THIS engagement (and viewable to ``actor``)? The reverse
        index (`host.list_jobs`) is the single source of that fact -- the SAME one the panel renders from.
        Un-adopt is scoped to it so a writer can't clear a job's link via the WRONG engagement's URL:
        `host.remove_job_adoption` takes only the job id, so without this guard an un-adopt of a job
        belonging to engagement B, POSTed at engagement A's route, would clear B's link. (The destructive
        delete is already engagement-scoped -- `enriched_findings` reads THIS engagement's rows -- so this
        guard is specifically about not touching another engagement's adoption.)"""
        return str(job_id) in {str(j.id) for j in host.list_jobs(engagement, actor)}

    def _job_finding_ids(job_id, actor) -> set:
        """The set of host scan-finding ids job ``job_id`` produced, via the read-only findings seam --
        the CORE contract that answers "which findings did this job enrich". Empty when unmounted or the
        job is not viewable to ``actor`` (indistinguishable, no existence leak), which safely selects
        nothing to remove."""
        ns = host.findings()
        if ns is None:
            return set()
        # Drop any None id: a synthesized parent finding also carries source_finding_id None, so a None
        # in this set would make `finding_is_enriched` select author-owned parents by accident.
        return {i for i in (getattr(dto, "id", None) for dto in ns.list_findings(job_id, actor))
                if i is not None}

    @bp.post("/engagements/<uuid:engagement_id>/unadopt-job/<job_id>", endpoint="unadopt_job")
    def unadopt_job(engagement_id: uuid.UUID, job_id: str):
        """Un-adopt LINK-ONLY (#635 path a): clear the job's promotion link, keep every promoted finding.
        The reverse of ``adopt_job``'s ``mark_job_promoted``. Nothing is deleted -- the poured findings
        stay exactly as the operator has since edited them -- so there is no preview and no
        deletion-audit; ``host.remove_job_adoption`` touches the link only (core #632). WRITE-gated;
        an unknown engagement 404s, same tenancy posture as its adopt twin."""
        if not host_can_write():
            abort(403)
        actor = current_actor()
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            act = _is_source_job(engagement, job_id, actor)
        if act:  # only clear a job actually adopted HERE; a foreign/unknown job is a silent no-op
            host.remove_job_adoption(job_id, actor)
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    @bp.get("/engagements/<uuid:engagement_id>/unadopt-job/<job_id>/preview",
            endpoint="unadopt_job_preview")
    def unadopt_job_preview(engagement_id: uuid.UUID, job_id: str):
        """Destructive un-adopt PREVIEW (#635 path b, step 1): the JSON list of the EXACT findings a
        destructive un-adopt would remove -- ``promote.enriched_findings``, the SAME set the destroy route
        deletes, so a confirmed destroy can never surprise the operator. A GET, so it is NOT write-gated
        (the tenancy contract treats every GET as a view; a viewer already sees these findings and their
        source jobs on the board -- correlating them is not a new leak). The destructive ACT is gated at
        the destroy POST, not here. Unknown engagement 404s."""
        from scribble.promote import enriched_findings  # lazy: promote.py is Track D's file
        actor = current_actor()
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            doomed = enriched_findings(engagement, _job_finding_ids(job_id, actor))
            return jsonify({
                "job_id": str(job_id),
                "findings": [{"id": str(f.id), "title": f.title} for f in doomed],
            })

    @bp.post("/engagements/<uuid:engagement_id>/unadopt-job/<job_id>/destroy",
             endpoint="unadopt_job_destroy")
    def unadopt_job_destroy(engagement_id: uuid.UUID, job_id: str):
        """Destructive un-adopt (#635 path b, step 2): remove EXACTLY the findings this job enriched
        (``enriched_findings`` -- the same set the preview listed), write ONE audit row naming the ids
        that went, then clear the link. ``findings_service.delete_finding`` takes each row's artifacts
        with it and detaches any children; files unlink only AFTER the commit, so a rolled-back delete
        can't leave bytes gone. WRITE-gated; unknown engagement 404s."""
        if not host_can_write():
            abort(403)
        from scribble.api_pat import _audit  # lazy: _audit + the host audit seam live in api_pat.py
        from scribble.promote import enriched_findings  # lazy: promote.py is Track D's file
        actor = current_actor()
        storage_paths: list = []
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                abort(404)
            if not _is_source_job(engagement, job_id, actor):
                return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))
            doomed = enriched_findings(engagement, _job_finding_ids(job_id, actor))
            removed_ids = [str(f.id) for f in doomed]
            for finding in doomed:
                storage_paths.extend(findings_service.delete_finding(db, finding).storage_paths)
            _audit(db, "unadopt_job_destructive", subject_type="engagement", subject_id=engagement.id,
                   before={"job_id": str(job_id), "removed_finding_ids": removed_ids})
            db.commit()
        host.remove_job_adoption(job_id, actor)
        for storage_path in storage_paths:
            delete_file(storage_path)
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    # =============================================================================== UI: delete finding

    @bp.post(
        "/engagements/<uuid:engagement_id>/findings/<uuid:finding_id>/delete", endpoint="delete_finding"
    )
    def delete_finding(engagement_id: int, finding_id: int):
        with open_session() as db:
            finding = db.get(BoardFinding, finding_id)
            if finding is None or finding.engagement_id != engagement_id:
                abort(404)
            # Takes its artifact ROWS with it (unlike delete_group's detach) and hands back their on-disk
            # paths — see findings_service.delete_finding. The files go only AFTER the commit below, so a
            # rolled-back delete cannot leave the bytes gone (mirrors artifacts_api.delete_artifact).
            # Children (per-host instances of a promoted vuln type) are DETACHED, not deleted — see
            # findings_service.detach_children. Without that step this route 500'd on any promoted parent.
            storage_paths = findings_service.delete_finding(db, finding).storage_paths
            db.commit()
        for storage_path in storage_paths:
            delete_file(storage_path)
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    # =============================================================================== UI: finding detail

    @bp.route("/findings/<uuid:finding_id>", methods=["GET", "POST"], endpoint="finding_detail")
    def finding_detail(finding_id: int):
        with open_session() as db:
            finding = db.get(BoardFinding, finding_id)
            if finding is None:
                abort(404)

            if request.method == "POST":
                title = (request.form.get("title") or "").strip()
                if title:
                    finding.title = title
                finding.category = (request.form.get("category") or "").strip() or None

                sev = request.form.get("severity")
                if sev:
                    try:
                        finding.severity = severity_enum()(sev)
                    except ValueError:
                        pass
                conf = request.form.get("confidence")
                if conf:
                    try:
                        finding.confidence = Confidence(conf)
                    except ValueError:
                        pass
                status = request.form.get("status")
                if status:
                    try:
                        finding.status = FindingStatus(status)
                    except ValueError:
                        pass

                finding.cvss_score = _parse_float(request.form.get("cvss_score"))
                finding.cvss_vector = (request.form.get("cvss_vector") or "").strip() or None
                finding.target_host = (request.form.get("target_host") or "").strip() or None
                finding.target_port = (request.form.get("target_port") or "").strip() or None
                finding.target_url = (request.form.get("target_url") or "").strip() or None
                finding.include_in_report = "include_in_report" in request.form
                # Report composition: the sections the operator chose to omit from the deliverable. Only
                # known keys are stored (a stray form value can't invent a section).
                allowed = {*schema.DEFAULT_BLOCKS, "affected_assets", "evidence", "references"}
                finding.suppressed_sections = [
                    s for s in request.form.getlist("suppress") if s in allowed
                ]
                db.commit()
                return redirect(url_for("scribble.finding_detail", finding_id=finding_id))

            blocks = list(schema.DEFAULT_BLOCKS)
            for extra in sorted((finding.content_json or {}).keys()):
                if extra not in blocks:
                    blocks.append(extra)
            gallery_artifacts = sorted(finding.artifacts, key=lambda a: a.order_index)
            variable_keys = sorted(known_variable_keys(db))
            # Affected-hosts editor: the finding's per-host CHILD rows (parent_id == this finding). Listed
            # WITHOUT the include_in_report filter on purpose — a SUPPRESSED host must stay visible here so
            # the operator can re-include it (the board keeps it too; only the renderers drop it).
            affected_children = list(db.scalars(
                select(BoardFinding)
                .where(BoardFinding.parent_id == finding.id)
                .order_by(BoardFinding.order_index)
            ))
            suppressible_sections = [*schema.DEFAULT_BLOCKS, "affected_assets", "evidence", "references"]

            return render_template(
                "scribble/finding.html",
                finding=finding,
                blocks=blocks,
                severities=list(severity_enum()),
                confidences=list(Confidence),
                statuses=list(FindingStatus),
                gallery_finding_id=finding.id,
                gallery_engagement_id=finding.engagement_id,
                gallery_artifacts=gallery_artifacts,
                scribble_variable_keys=variable_keys,
                retests=sorted(finding.retests, key=lambda r: r.created_at),
                retest_outcomes=list(RetestOutcome),
                affected_children=affected_children,
                suppressible_sections=suppressible_sections,
                suppressed_set=set(finding.suppressed_sections or []),
            )

    # ================================================================= POST: per-host report disposition

    @bp.route(
        "/findings/<uuid:finding_id>/hosts/disposition",
        methods=["POST"], endpoint="finding_host_disposition",
    )
    def finding_host_disposition(finding_id: int):
        """Include / suppress / remove ONE affected-host child of a finding. suppress = the child's
        ``include_in_report=False`` (dropped from the report, kept in the editor); remove = delete the
        child row (the false-positive case); include = re-add it.

        The target child is named in the FORM (``host_id``), not the URL, so the route is scoped by its
        one ``finding_id`` path arg — the engagement gate resolves membership from that. An unresolvable
        host id or unknown action is a graceful no-op redirect (like ``adopt_job``'s unknown-job path),
        never the gate's 404 denial signal."""
        action = (request.form.get("action") or "").strip()
        host_id = _as_uuid(request.form.get("host_id"))
        storage_paths: list[str] = []
        with open_session() as db:
            parent = db.get(BoardFinding, finding_id)
            if parent is None:
                abort(404)  # the SCOPED id — a real 404, consistent with every other finding route
            child = db.get(BoardFinding, host_id) if host_id is not None else None
            if child is not None and child.parent_id == parent.id:
                if action == "include":
                    child.include_in_report = True
                elif action == "suppress":
                    child.include_in_report = False
                elif action == "remove":
                    storage_paths = findings_service.delete_finding(db, child).storage_paths
                db.commit()
        for storage_path in storage_paths:
            delete_file(storage_path)
        return redirect(url_for("scribble.finding_detail", finding_id=finding_id))

    # =================================================================== POST: add an affected host row

    @bp.route("/findings/<uuid:finding_id>/hosts", methods=["POST"], endpoint="finding_add_host")
    def finding_add_host(finding_id: int):
        """Add a per-host child to a finding — the operator manually recording an affected asset that the
        scan did not associate. Inherits the parent's vuln identity (title/severity/category); carries its
        own target."""
        with open_session() as db:
            parent = db.get(BoardFinding, finding_id)
            if parent is None:
                abort(404)
            host_val = (request.form.get("target_host") or "").strip()
            if host_val:
                db.add(BoardFinding(
                    engagement_id=parent.engagement_id, group_id=parent.group_id, parent_id=parent.id,
                    title=parent.title, severity=parent.severity, category=parent.category,
                    target_host=host_val,
                    target_port=(request.form.get("target_port") or "").strip() or None,
                    target_url=(request.form.get("target_url") or "").strip() or None,
                    content_json={}, order_index=(parent.order_index or 0) + 1,
                ))
                db.commit()
        return redirect(url_for("scribble.finding_detail", finding_id=finding_id))

    # ============================================================================ POST: record a retest

    @bp.route(
        "/findings/<uuid:finding_id>/retest", methods=["POST"], endpoint="record_finding_retest"
    )
    def record_finding_retest(finding_id: int):
        # The blueprint-wide gate (scribble/authz.py) already refuses a non-safe method without write,
        # keyed on the same finding_id view arg it scopes tenancy by; the explicit check mirrors the
        # sibling write routes (delete_finding, add_finding) as defence in depth.
        if not host_can_write():
            abort(403)
        with open_session() as db:
            finding = db.get(BoardFinding, finding_id)
            if finding is None:
                abort(404)
            try:
                outcome = RetestOutcome((request.form.get("outcome") or "").strip())
            except ValueError:
                abort(400, "unknown retest outcome")
            # Status transition (and a not_tested no-op) lives in the ONE writer, not here.
            findings_service.record_retest(
                db,
                finding,
                outcome,
                notes=(request.form.get("notes") or "").strip() or None,
                # Cap to the column width (models.Retest.tested_by is String(128)); an over-long value
                # is a DataError -> 500 on Postgres (SQLite silently truncates), so bound it here.
                tested_by=(request.form.get("tested_by") or "").strip()[:128] or None,
                tested_on=_parse_date(request.form.get("tested_on")),
            )
            db.commit()
            return redirect(url_for("scribble.finding_detail", finding_id=finding_id))

    # =============================================================================== API: reorder groups

    @api_bp.post("/engagements/<uuid:engagement_id>/groups/reorder")
    def reorder_groups(engagement_id: int):
        payload = request.get_json(silent=True) or {}
        order = payload.get("order")
        if not isinstance(order, list):
            return jsonify(error="order must be a list of group ids"), 400

        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                return jsonify(error="engagement not found"), 404

            # Stale/foreign/duplicate ids are ignored and unmentioned groups keep their relative order at
            # the end — see findings_service.reorder_groups (UUID-aware since lotek#335 — it parses each
            # client-supplied id with the same `_as_uuid` this module uses, not `int`).
            ordered_ids = findings_service.reorder_groups(engagement, order)
            db.commit()

            result = [{"id": gid, "order_index": index} for index, gid in enumerate(ordered_ids)]
        return jsonify(ok=True, order=result)

    # =========================================================================== API: report section order

    @api_bp.post("/engagements/<uuid:engagement_id>/report/sections")
    def reorder_report_sections(engagement_id: int):
        """Persist the per-report SECTION ORDER + on/off (``ReportBoard.section_order``). Body:
        ``{"order": [{"key": <block>, "enabled": bool}, ...]}``. The value is normalized through
        ``layouts.resolve_section_order`` before storing (unknown keys dropped, dups collapsed, any missing
        block appended disabled), so what is persisted is always a complete, valid list against BLOCK_KEYS —
        the SAME resolver both renderers read, so the editor, the HTML preview and the DOCX deliverable can't
        disagree. Session-authed like its sibling ``reorder_groups``; a reorder leaks nothing, it only
        rearranges the caller's own report layout."""
        from scribble.reporting.layouts import resolve_section_order

        payload = request.get_json(silent=True) or {}
        order = payload.get("order")
        if not isinstance(order, list):
            return jsonify(error="order must be a list of {key, enabled}"), 400
        normalized = [{"key": s.key, "enabled": s.enabled} for s in resolve_section_order(order)]
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                return jsonify(error="engagement not found"), 404
            engagement.section_order = normalized
            db.commit()
        return jsonify(ok=True, order=normalized)

    # ======================================================================= API/UI: saved section presets

    @api_bp.post("/report/section-presets")
    def save_section_preset():
        """Save the current section arrangement as an ORG-WIDE named preset (#Q10). Body:
        ``{"name": str, "order": [{"key","enabled"}, ...]}``. The arrangement is normalized + fingerprinted;
        a duplicate (identical order AND visibility) is refused 409 and names the existing preset, so two
        operators can't save the same sequence twice.

        SELF-GATED: this route has no engagement in its URL, so ``authz._gate`` skips its write-capability
        check — the preset library is org-wide, so writing it requires write capability (not ownership;
        any operator manages the shared library, per #Q10), re-checked here."""
        from scribble.reporting.layouts import resolve_section_order, section_fingerprint

        if not host_can_write():
            abort(403)
        payload = request.get_json(silent=True) or {}
        name = (payload.get("name") or "").strip()[:120]
        order = payload.get("order")
        if not name:
            return jsonify(error="name is required"), 400
        if not isinstance(order, list):
            return jsonify(error="order must be a list of {key, enabled}"), 400
        specs = [{"key": s.key, "enabled": s.enabled} for s in resolve_section_order(order)]
        fingerprint = section_fingerprint(order)
        with open_session() as db:
            existing = db.scalar(
                select(ScribbleSectionPreset).where(ScribbleSectionPreset.fingerprint == fingerprint)
            )
            if existing is not None:
                return jsonify(
                    error="duplicate",
                    detail=f"This exact arrangement is already saved as “{existing.name}”.",
                    preset={"id": str(existing.id), "name": existing.name},
                ), 409
            preset = ScribbleSectionPreset(name=name, specs=specs, fingerprint=fingerprint,
                                           created_by=current_actor_username())
            db.add(preset)
            try:
                db.commit()
            except IntegrityError:
                # Two operators saved the identical arrangement concurrently: the check above passed for
                # both, the unique `fingerprint` index rejects the loser. Same 409 the check-path returns.
                db.rollback()
                existing = db.scalar(
                    select(ScribbleSectionPreset).where(ScribbleSectionPreset.fingerprint == fingerprint)
                )
                return jsonify(
                    error="duplicate",
                    detail=(f"This exact arrangement is already saved as “{existing.name}”."
                            if existing is not None else "This exact arrangement is already saved."),
                    preset=({"id": str(existing.id), "name": existing.name}
                            if existing is not None else None),
                ), 409
            result = {"id": str(preset.id), "name": preset.name, "specs": specs}
        return jsonify(ok=True, preset=result)

    @bp.post("/report/section-presets/<uuid:preset_id>/delete", endpoint="delete_section_preset")
    def delete_section_preset(preset_id: int):
        """Delete an org-wide saved section preset. Plain form POST; redirects back.

        SELF-GATED for the same reason as ``save_section_preset``: no engagement in the URL, so the
        blueprint gate's write-capability check doesn't apply — re-checked here so a viewer can't delete
        the shared preset library."""
        if not host_can_write():
            abort(403)
        with open_session() as db:
            preset = db.get(ScribbleSectionPreset, preset_id)
            if preset is not None:
                db.delete(preset)
                db.commit()
        return redirect(request.referrer or url_for("scribble.dashboard"))

    # ============================================================================ API/UI: cover logo

    @bp.post("/engagements/<uuid:engagement_id>/report/logo", endpoint="set_report_logo")
    def set_report_logo(engagement_id: int):
        """Pick this report's cover LOGO from the org-wide library — a plain form POST (``logo_id`` field;
        empty or ``"default"`` => the stock lotek mark). The pick only changes this report's own cover."""
        raw = (request.form.get("logo_id") or "").strip()
        clear = raw in ("", "default")
        logo_id = None if clear else _as_uuid(raw)
        if not clear and logo_id is None:
            abort(400)
        with open_session() as db:
            eng = db.get(ReportBoard, engagement_id)
            if eng is None:
                abort(404)
            if logo_id is not None and db.get(ScribbleReportLogo, logo_id) is None:
                abort(404)
            eng.cover_logo_id = logo_id
            db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    @bp.post("/report/logos", endpoint="upload_report_logo")
    def upload_report_logo():
        """Add an image to the ORG-WIDE cover-logo library (every operator can then pick it) and, when the
        form names an engagement, set it as that report's cover in the same step (upload-and-use). Raster
        images only, bounded by ``_MAX_LOGO_BYTES``.

        SELF-GATED (INV-TENANCY-05/06). This route carries no engagement id in its URL, so the
        blueprint-wide tenancy gate (``authz._gate``) skips it entirely — both the write-capability check
        AND, when the body names an engagement, the view check its ``set_report_logo`` sibling gets for
        free from the URL. Both are re-applied here by hand: (1) writing the shared library requires write
        capability; (2) the ``engagement_id`` arrives in the FORM BODY, so it is authorized directly via
        ``can_view_engagement`` — matching the body-scoped ``create_artifact``/``templating_preview``
        pattern — before its ``cover_logo_id`` is touched, or a viewer could set any tenant's cover."""
        if not host_can_write():
            abort(403)
        upload = request.files.get("file")
        filename = (upload.filename or "").strip() if upload is not None else ""
        if upload is None or not filename:
            abort(400)
        data = upload.read()
        if not data:
            abort(400)
        if len(data) > _MAX_LOGO_BYTES:
            abort(413)
        content_type = guess_content_type(filename, data)
        if content_type not in _LOGO_TYPES:
            abort(400)  # raster images only (no SVG: it serves inline and can carry script)
        label = ((request.form.get("label") or "").strip() or filename)[:120]
        raw_eng = (request.form.get("engagement_id") or "").strip()
        eng_id = _as_uuid(raw_eng) if raw_eng else None
        with open_session() as db:
            logo = ScribbleReportLogo(label=label, content_type=content_type, data=data,
                                      created_by=current_actor_username())
            db.add(logo)
            db.flush()
            if eng_id is not None:
                eng = db.get(ReportBoard, eng_id)
                # Fail closed like the gate: a foreign-but-real engagement gets the same 404 as a
                # nonexistent one (no cross-tenant existence oracle), and the cover is only set once the
                # caller is proven able to view it.
                if eng is None or not can_view_engagement(eng, current_actor()):
                    abort(404)
                eng.cover_logo_id = logo.id  # upload-and-use on this report
            db.commit()
        if eng_id is not None:
            return redirect(url_for("scribble.engagement_board", engagement_id=eng_id))
        return redirect(request.referrer or url_for("scribble.dashboard"))

    @bp.get("/report/logos/default/raw", endpoint="report_logo_default_raw")
    def report_logo_default_raw():
        """Serve the stock lotek mark — the default cover logo and the picker's 'Default' thumbnail."""
        from scribble.reporting.logos import DEFAULT_LOGO_CONTENT_TYPE, default_logo_bytes

        resp = send_file(io.BytesIO(default_logo_bytes()), mimetype=DEFAULT_LOGO_CONTENT_TYPE,
                         max_age=3600, download_name="lotek.png")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @bp.get("/report/logos/<uuid:logo_id>/raw", endpoint="report_logo_raw")
    def report_logo_raw(logo_id: int):
        """Serve a library logo's bytes — the cover image and the picker thumbnail. Inline (raster only, so
        no inline-script surface) with nosniff."""
        with open_session() as db:
            logo = db.get(ScribbleReportLogo, logo_id)
            if logo is None:
                abort(404)
            resp = send_file(io.BytesIO(logo.data), mimetype=logo.content_type or "image/png",
                             max_age=3600, download_name=f"{logo.label or 'logo'}")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    # ============================================================================ API/UI: report prose

    @bp.post("/engagements/<uuid:engagement_id>/report/prose", endpoint="save_report_prose")
    def save_report_prose(engagement_id: int):
        """Save this report's Methodology / Scope-and-limitations override text (#Q4). An empty field RESETS
        that section to the standing text (stored NULL). Plain form POST."""
        methodology = (request.form.get("methodology_text") or "").strip()
        scope = (request.form.get("scope_limitations_text") or "").strip()
        with open_session() as db:
            eng = db.get(ReportBoard, engagement_id)
            if eng is None:
                abort(404)
            eng.methodology_text = methodology or None      # "" => reset to the generated standing text
            eng.scope_limitations_text = scope or None
            db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    @api_bp.post("/report/prose/rephrase")
    def rephrase_report_prose():
        """Rephrase operator prose through the host AI hook and return ``{text: rewrite}`` for the operator to
        review and save (never a silent in-place overwrite). 503 when the host provides no AI hook (it is
        settings-gated + default OFF), so the button simply isn't offered then.

        SELF-GATED: no engagement in the URL, so ``authz._gate`` skips its write-capability check. This
        route proxies an operator's text to the host LLM, so it requires write capability — that keeps a
        read-only viewer from driving the (metered) AI hook.
        ponytail: write-cap bounds abuse to trusted operators; a per-actor rate limit would need shared
        state this extension doesn't own — add one if operator-side abuse ever shows up."""
        if not host_can_write():
            abort(403)
        ai = host.host_hook("ai_complete")
        if ai is None:
            return jsonify(error="ai_unavailable", detail="AI is not enabled on this install"), 503
        payload = request.get_json(silent=True) or {}
        text = (payload.get("text") or "").strip()
        if not text:
            return jsonify(error="text is required"), 400
        if len(text) > _MAX_REPHRASE_CHARS:
            return jsonify(error="text too long"), 413
        section = {"methodology": "penetration-test methodology",
                   "scope": "scope and limitations"}.get((payload.get("field") or "").strip(), "report")
        messages = [
            {"role": "system", "content": "You rewrite security-assessment report prose to be clear, "
             "professional and concise. Return ONLY the rewritten text — no preamble, no markdown fences."},
            {"role": "user", "content": f"Rephrase this {section} section:\n\n{text}"},
        ]
        try:
            rewritten = ai(messages)
        except Exception:  # host AI failed/disabled at call time — 502, don't 500
            # Do NOT echo str(exc): the host AI client's exception text can carry provider URLs, model
            # names or key fragments. A fixed message is all the operator needs; details go to the log.
            import logging
            logging.getLogger(__name__).exception("scribble: ai_complete rephrase failed")
            return jsonify(error="ai_failed", detail="The AI service could not be reached."), 502
        return jsonify(ok=True, text=(rewritten or "").strip())

    # =============================================================================== API: move finding

    @api_bp.post("/findings/<uuid:finding_id>/move")
    def move_finding(finding_id: int):
        payload = request.get_json(silent=True) or {}
        if "group_id" not in payload:
            return jsonify(error="group_id is required (use null to move to ungrouped)"), 400

        raw_group_id = payload.get("group_id")
        requested_index = _as_int(payload.get("order_index", 0))
        if requested_index is None:
            return jsonify(error="order_index must be an integer"), 400

        with open_session() as db:
            finding = db.get(BoardFinding, finding_id)
            if finding is None:
                return jsonify(error="finding not found"), 404

            target_group = None
            if raw_group_id is not None:
                target_group_id = _as_uuid(raw_group_id)
                if target_group_id is None:
                    return jsonify(error="group_id must be a UUID or null"), 400
                target_group = db.get(FindingGroup, target_group_id)
                if target_group is None or target_group.engagement_id != finding.engagement_id:
                    return jsonify(error=f"group {target_group_id} not found on this engagement"), 404

            # The ordering rules (client index = a slot in the RENDERED order; reindex both sides; flip
            # the destination group to manual) live in findings_service.place_finding, which the machine
            # API's move routes call too — see its docstring. It returns the PREVIOUS group when the move
            # crossed groups, which is what this response reports as `previous_group`.
            previous_group = findings_service.place_finding(finding, target_group, requested_index)

            db.commit()

            response = {
                "ok": True,
                "finding": {
                    "id": finding.id,
                    "group_id": finding.group_id,
                    "order_index": finding.order_index,
                },
                "group": (
                    {"id": target_group.id, "order_mode": target_group.order_mode.value}
                    if target_group is not None
                    else None
                ),
                "previous_group": (
                    {"id": previous_group.id, "order_mode": previous_group.order_mode.value}
                    if previous_group is not None
                    else None
                ),
            }
        return jsonify(response)

    # ============================================================================ API: move findings (bulk)

    @api_bp.post("/engagements/<uuid:engagement_id>/findings/move")
    def move_findings(engagement_id: int):
        """Move SEVERAL findings into one group at once — the cookie sibling of the machine API's
        ``scribble_move_findings`` (api_pat.py), for the board's multi-select bulk bar. Body:
        ``{"finding_ids": [...], "group_id": <uuid|null>, "order_index": <int>}``.

        ATOMIC like the machine route: every id must belong to THIS engagement or the whole request is
        refused (same ``finding not found`` a foreign id gets) and nothing moves — a partial success
        would be indistinguishable from a complete one, and skipping is what makes a foreign id a probe.
        Duplicate ids (a multi-select artefact) collapse to their first occurrence; the listed order is
        preserved (each finding lands at ``order_index + its position``)."""
        payload = request.get_json(silent=True) or {}
        if "group_id" not in payload:
            return jsonify(error="group_id is required (use null for ungrouped)"), 400
        raw_ids = payload.get("finding_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify(error="finding_ids must be a non-empty list"), 400
        # Bound the walk BEFORE touching the DB (mirrors the machine route's _BULK_ID_LIST_MAX).
        if len(raw_ids) > 500:
            return jsonify(error="finding_ids may contain at most 500 ids"), 400
        finding_ids: list[uuid.UUID] = []
        for raw in raw_ids:
            parsed = _as_uuid(raw)
            if parsed is None:
                return jsonify(error="finding_ids must contain UUIDs"), 400
            if parsed not in finding_ids:
                finding_ids.append(parsed)

        requested_index = _as_int(payload.get("order_index", 0))
        if requested_index is None:
            return jsonify(error="order_index must be an integer"), 400

        with open_session() as db:
            target_group = None
            raw_group_id = payload.get("group_id")
            if raw_group_id is not None:
                target_group_id = _as_uuid(raw_group_id)
                if target_group_id is None:
                    return jsonify(error="group_id must be a UUID or null"), 400
                target_group = db.get(FindingGroup, target_group_id)
                if target_group is None or target_group.engagement_id != engagement_id:
                    return jsonify(error=f"group {target_group_id} not found on this engagement"), 404

            # One query, all-or-nothing: the set difference is empty only if every id belongs to this
            # engagement. A foreign id gets the same 404 a nonexistent one does, and NOTHING moves.
            present = set(db.scalars(
                select(BoardFinding.id).where(
                    BoardFinding.id.in_(finding_ids),
                    BoardFinding.engagement_id == engagement_id,
                )
            ).all())
            if len(present) != len(finding_ids):
                return jsonify(error="finding not found"), 404

            placed = []
            for offset, fid in enumerate(finding_ids):
                finding = db.get(BoardFinding, fid)
                findings_service.place_finding(finding, target_group, requested_index + offset)
                placed.append(finding)
            # Read order_index AFTER every placement — each insert reindexes the destination.
            moved = [{"id": f.id, "group_id": f.group_id, "order_index": f.order_index} for f in placed]
            db.commit()
        return jsonify(ok=True, moved=moved,
                       group_id=str(target_group.id) if target_group is not None else None)

    # ======================================================================== attack paths (ext#141)
    # Link/unlink a vector attack-path diagram from the browser. Scribble has no seam to vector, so the
    # picker (board.js) fetches vector's cookie API to list the author's diagrams and its export.html,
    # then POSTs the self-contained snapshot here — the cookie sibling of api_pat's link/delete. The
    # report renders embed_html in a SANDBOXED iframe (render_html), so a stored snapshot is contained
    # exactly as the machine path already assumes.

    _MAX_DIAGRAM_HTML_BYTES = 10 * 1024 * 1024

    @api_bp.post("/engagements/<uuid:engagement_id>/attack-paths")
    def link_attack_path(engagement_id: int):
        payload = request.get_json(silent=True) or {}
        embed_html = payload.get("embed_html")
        if not isinstance(embed_html, str) or not embed_html.strip():
            return jsonify(error="embed_html is required"), 400
        if len(embed_html.encode("utf-8")) > _MAX_DIAGRAM_HTML_BYTES:
            return jsonify(error="embed_html exceeds the 10 MiB limit"), 413
        diagram_ref = (payload.get("diagram_ref") or None)
        if diagram_ref is not None:
            diagram_ref = str(diagram_ref)[:64]
        caption = (payload.get("caption") or None)
        if caption is not None:
            caption = str(caption)[:255]
        with open_session() as db:
            engagement = db.get(ReportBoard, engagement_id)
            if engagement is None:
                return jsonify(error="engagement not found"), 404
            diagram = EngagementDiagram(
                engagement_id=engagement_id, diagram_ref=diagram_ref, caption=caption,
                embed_html=embed_html, order_index=len(list(engagement.diagrams)),
                include_in_report=True,
            )
            db.add(diagram)
            db.flush()
            body = {"id": str(diagram.id), "caption": diagram.caption or "",
                    "diagram_ref": diagram.diagram_ref}
            db.commit()
        return jsonify(ok=True, **body)

    @bp.post("/engagements/<uuid:engagement_id>/attack-paths/<uuid:diagram_id>/unlink",
             endpoint="unlink_attack_path")
    def unlink_attack_path(engagement_id: int, diagram_id: int):
        if not host_can_write():
            abort(403)
        with open_session() as db:
            diagram = db.get(EngagementDiagram, diagram_id)
            # Scope to the URL engagement — a diagram id from another engagement is a 404, not a delete.
            if diagram is None or diagram.engagement_id != engagement_id:
                abort(404)
            db.delete(diagram)
            db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    # =============================================================================== API: update group

    @api_bp.post("/groups/<uuid:group_id>")
    def update_group(group_id: int):
        payload = request.get_json(silent=True) or {}
        with open_session() as db:
            group = db.get(FindingGroup, group_id)
            if group is None:
                return jsonify(error="group not found"), 404

            if "name" in payload:
                name = (payload.get("name") or "").strip()
                if not name:
                    return jsonify(error="name cannot be empty"), 400
                group.name = name

            if "order_mode" in payload:
                try:
                    group.order_mode = OrderMode(payload["order_mode"])
                except ValueError:
                    return jsonify(error=f"invalid order_mode {payload['order_mode']!r}"), 400

            if "include_in_report" in payload:
                group.include_in_report = bool(payload["include_in_report"])

            db.commit()
            result = {
                "id": group.id,
                "name": group.name,
                "order_mode": group.order_mode.value,
                "include_in_report": group.include_in_report,
                "order_index": group.order_index,
            }
        return jsonify(result)
