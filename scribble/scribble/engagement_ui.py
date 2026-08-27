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

from scribble import findings_service
from scribble.artifacts_storage import artifact_ref, delete_file
from scribble.authz import can_view_client_id, host_is_mounted, visible_engagements
from scribble.content import schema
from scribble.deps import (
    client_model,
    client_names,
    current_actor,
    current_actor_id,
    current_actor_username,
    get_config,
    open_session,
    severity_enum,
)
from scribble.enums import Confidence, FindingStatus, OrderMode
from scribble.models import (
    AssessmentType,
    Engagement,
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

    engagement.name = (form.get("name") or "").strip()
    engagement.scope_type = (form.get("scope_type") or "external").strip() or "external"
    engagement.company_name = (form.get("company_name") or "").strip() or None
    engagement.status = (form.get("status") or "in_progress").strip() or "in_progress"
    engagement.start_date = _parse_date(form.get("start_date"))
    engagement.end_date = _parse_date(form.get("end_date"))
    engagement.client_id = client_id
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

                engagement = Engagement(
                    name=name,
                    client_id=client_id,
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
        cfg = get_config()
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            # Mirrors delete_finding's explicit artifact-file cleanup below: the ORM cascade
            # (Engagement.artifacts, cascade="all, delete-orphan") removes the Artifact ROWS, but the
            # bytes on disk are not the ORM's to clean up -- collect the paths before the cascade delete,
            # then best-effort remove the files afterward, same as delete_finding does per-finding.
            # artifact_ref, not the raw path: deleting an engagement must not leave its evidence
            # blobs behind in the object store with every row that referenced them gone.
            storage_paths = [artifact_ref(a) for a in engagement.artifacts]
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
            delete_file(cfg, storage_path)
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

            return render_template(
                "scribble/engagement.html",
                engagement=engagement,
                client_name=(client.name if client is not None else None),
                board_groups=board_groups,
                ungrouped=ungrouped,
                templates=templates,
                assessment_types=assessment_types,
                engagement_artifacts=engagement_artifacts,
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

    # =============================================================================== UI: delete finding

    @bp.post(
        "/engagements/<uuid:engagement_id>/findings/<uuid:finding_id>/delete", endpoint="delete_finding"
    )
    def delete_finding(engagement_id: int, finding_id: int):
        cfg = get_config()
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
            delete_file(cfg, storage_path)
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
