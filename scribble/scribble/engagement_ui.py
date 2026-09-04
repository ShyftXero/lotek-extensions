"""Engagement + finding-board UI (WS3 owns this module).

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
                                                          on ``Engagement`` in models.py.
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

``Engagement.created_by`` / ``EngagementFinding.created_by`` are set from the optional host-injected
``current_actor`` hook (``scribble.deps.current_actor_username``) -- ``None`` standalone.
"""

from __future__ import annotations

import uuid
from datetime import date

from flask import abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy import select

from scribble import findings_service, host
from scribble.artifacts_storage import delete_file
from scribble.authz import can_view_client_id, host_is_mounted, visible_engagements
from scribble.content import schema
from scribble.deps import (
    client_model,
    client_names,
    current_actor,
    current_actor_id,
    current_actor_username,
    host_can_write,
    open_session,
    severity_enum,
)
from scribble.enums import Confidence, FindingStatus, OrderMode
from scribble.models import (
    AssessmentType,
    Engagement,
    EngagementDiagram,
    EngagementFinding,
    FindingGroup,
    VulnerabilityTemplate,
)
from scribble.templating import known_variable_keys

_REGISTERED = False


# --------------------------------------------------------------------------------- small helpers


from scribble.artifacts_api import _as_uuid  # noqa: E402  -- one shared body-id parser (lotek#335)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_id(value) -> int | uuid.UUID | None:
    """Parse a form-submitted ``client_id`` tolerantly: EITHER host id shape ``Engagement.client_id``
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

    Mounted, the granted id is returned WITHOUT looking the row up: ``Engagement.client_id`` is a soft
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


def _apply_engagement_form(engagement: Engagement, form, db) -> str | None:
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
    return None


def register(api_bp, bp) -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # =============================================================================== UI: engagements

    @bp.get("/engagements", endpoint="engagements")
    def engagements():
        """The engagement list, scoped to the viewer's own clients.

        It listed every engagement in the database, for every client -- the same cross-tenant read the
        by-id gate closes, minus the need to guess an id. See `scribble.authz.visible_engagements`
        and `blueprint.dashboard`, which is the same fix on the same data.
        """
        with open_session() as db:
            visible = visible_engagements(
                db, select(Engagement).order_by(Engagement.created_at.desc()), current_actor()
            )
            return render_template(
                "scribble/engagements.html",
                engagements=visible,
                client_names=client_names(db, visible),
            )

    @bp.route("/engagements/new", methods=["GET", "POST"], endpoint="engagement_new")
    def engagement_new():
        with open_session() as db:
            if request.method == "POST":
                name = (request.form.get("name") or "").strip()
                if not name:
                    return (
                        render_template(
                            "scribble/engagement_new.html",
                            clients=_viewable_clients(db),
                            error="Name is required.",
                        ),
                        400,
                    )

                # Tenancy: the client comes from the form, so no id-shaped gate reaches it -- see
                # `_resolve_client` for the three rules and why each exists.
                client_id, error = _resolve_client(db, request.form)
                if error is not None:
                    return (
                        render_template(
                            "scribble/engagement_new.html",
                            clients=_viewable_clients(db),
                            error=error,
                        ),
                        400,
                    )

                # THE ANCHOR — see api_pat's create for the full reasoning. Obtained at CREATE time
                # so an upload never finds it missing. Creating one is manager-or-admin in the host,
                # so an operator using the browser gets a plain, actionable refusal rather than a
                # working engagement whose evidence has nowhere to go.
                # Not gated on `host_is_mounted()`: storage and authorization are separate host
                # capabilities, and evidence needs its anchor wherever an object store exists.
                try:
                    core_engagement_id = host.create_engagement(client_id, name)
                except PermissionError:
                    return (
                        render_template(
                            "scribble/engagement_new.html",
                            clients=_viewable_clients(db),
                            error="Creating an engagement requires manager or admin. Ask one to "
                                  "create it, or create it in lotek first.",
                        ),
                        403,
                    )
                except ValueError:
                    return (
                        render_template(
                            "scribble/engagement_new.html",
                            clients=_viewable_clients(db),
                            error="An engagement with this name already exists for this client.",
                        ),
                        409,
                    )

                engagement = Engagement(
                    name=name,
                    client_id=client_id,
                    core_engagement_id=core_engagement_id,
                    scope_type=(request.form.get("scope_type") or "external").strip() or "external",
                    company_name=(request.form.get("company_name") or "").strip() or None,
                    start_date=_parse_date(request.form.get("start_date")),
                    end_date=_parse_date(request.form.get("end_date")),
                    created_by=current_actor_username(),
                    owner_id=current_actor_id(),
                )
                db.add(engagement)
                db.commit()
                return redirect(url_for("scribble.engagement_board", engagement_id=engagement.id))

            return render_template(
                "scribble/engagement_new.html", clients=_viewable_clients(db), error=None
            )

    # =============================================================================== UI: edit / delete

    @bp.get("/engagements/<uuid:engagement_id>/edit", endpoint="engagement_edit_page")
    def engagement_edit_page(engagement_id: int):
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
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
            engagement = db.get(Engagement, engagement_id)
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
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            # Mirrors delete_finding's explicit artifact-file cleanup below: the ORM cascade
            # (Engagement.artifacts, cascade="all, delete-orphan") removes the Artifact ROWS, but the
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
            engagement = db.get(Engagement, engagement_id)
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

            # Engagement-level evidence (ext#51): artifacts attached to the engagement itself, not to
            # any finding (``finding_id`` null). These render into the client report's Evidence
            # appendix (ext#40 / ``reporting/context.py``'s ``ReportContext.artifacts``) but previously
            # had no UI review/exclude surface -- an operator could only discover what published by
            # reading the rendered report.
            engagement_artifacts = sorted(
                (a for a in engagement.artifacts if a.finding_id is None),
                key=lambda a: (a.order_index, a.id),
            )

            diagrams = sorted(engagement.diagrams, key=lambda d: (d.order_index, d.id))

            return render_template(
                "scribble/engagement.html",
                engagement=engagement,
                client_name=(client.name if client is not None else None),
                board_groups=board_groups,
                ungrouped=ungrouped,
                templates=templates,
                assessment_types=assessment_types,
                engagement_artifacts=engagement_artifacts,
                diagrams=diagrams,
            )

    # =============================================================================== UI: groups

    @bp.post("/engagements/<uuid:engagement_id>/groups", endpoint="create_group")
    def create_group(engagement_id: int):
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
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
            engagement = db.get(Engagement, engagement_id)
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
                finding = EngagementFinding.from_template(
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
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            findings_ns = host.findings()
            job = findings_ns.get_job(job_id, actor) if (job_id and findings_ns is not None) else None
            if job is not None:
                from scribble.promote import promote_job  # lazy: promote.py is Track D's file
                dtos = findings_ns.list_findings(job_id, actor)
                promote_job(db, engagement=engagement, findings=dtos,
                            actor_username=current_actor_username())
                db.commit()
                promoted_ref = engagement.id  # capture inside the session for the host-side write below
        if promoted_ref is not None:
            # The one host-contract write, in its own transaction (mirrors the machine route).
            host.mark_job_promoted(job_id, actor, extension="scribble", ref_id=promoted_ref)
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    # =============================================================================== UI: delete finding

    @bp.post(
        "/engagements/<uuid:engagement_id>/findings/<uuid:finding_id>/delete", endpoint="delete_finding"
    )
    def delete_finding(engagement_id: int, finding_id: int):
        with open_session() as db:
            finding = db.get(EngagementFinding, finding_id)
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
            finding = db.get(EngagementFinding, finding_id)
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
                db.commit()
                return redirect(url_for("scribble.finding_detail", finding_id=finding_id))

            blocks = list(schema.DEFAULT_BLOCKS)
            for extra in sorted((finding.content_json or {}).keys()):
                if extra not in blocks:
                    blocks.append(extra)
            gallery_artifacts = sorted(finding.artifacts, key=lambda a: a.order_index)
            variable_keys = sorted(known_variable_keys(db))

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
            )

    # =============================================================================== API: reorder groups

    @api_bp.post("/engagements/<uuid:engagement_id>/groups/reorder")
    def reorder_groups(engagement_id: int):
        payload = request.get_json(silent=True) or {}
        order = payload.get("order")
        if not isinstance(order, list):
            return jsonify(error="order must be a list of group ids"), 400

        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                return jsonify(error="engagement not found"), 404

            # Stale/foreign/duplicate ids are ignored and unmentioned groups keep their relative order at
            # the end — see findings_service.reorder_groups (UUID-aware since lotek#335 — it parses each
            # client-supplied id with the same `_as_uuid` this module uses, not `int`).
            ordered_ids = findings_service.reorder_groups(engagement, order)
            db.commit()

            result = [{"id": gid, "order_index": index} for index, gid in enumerate(ordered_ids)]
        return jsonify(ok=True, order=result)

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
            finding = db.get(EngagementFinding, finding_id)
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
                select(EngagementFinding.id).where(
                    EngagementFinding.id.in_(finding_ids),
                    EngagementFinding.engagement_id == engagement_id,
                )
            ).all())
            if len(present) != len(finding_ids):
                return jsonify(error="finding not found"), 404

            placed = []
            for offset, fid in enumerate(finding_ids):
                finding = db.get(EngagementFinding, fid)
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
            engagement = db.get(Engagement, engagement_id)
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
