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
                                                           finding IS its content, so this does not detach
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
content and evidence, not a container for other authored rows; 404 (never 500) if the finding doesn't
exist or belongs to a different engagement, mirroring ``delete_group``'s guard.

``Engagement.created_by`` / ``EngagementFinding.created_by`` are set from the optional host-injected
``current_actor`` hook (``scribble.deps.current_actor_username``) -- ``None`` standalone.
"""

from __future__ import annotations

import uuid
from datetime import date

from flask import abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy import select

from scribble.artifacts_storage import delete_file
from scribble.content import schema
from scribble.deps import (
    client_model,
    client_names,
    current_actor_id,
    current_actor_username,
    get_config,
    open_session,
    severity_enum,
)
from scribble.enums import Confidence, FindingStatus, OrderMode, severity_rank
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


def _display_order(findings, order_mode: OrderMode) -> list[EngagementFinding]:
    """Board display order for one group's findings.

    Mirrors ``reporting/context.py::_order_findings`` (auto_severity = worst-first then order_index;
    manual = order_index only) but WITHOUT the ``include_in_report`` filter — authors need to see (and
    re-include) excluded findings on the board, unlike the rendered report.
    """
    items = list(findings)
    if order_mode == OrderMode.manual:
        return sorted(items, key=lambda f: f.order_index)
    return sorted(items, key=lambda f: (severity_rank(f.severity), f.order_index))


def _reindex(items) -> None:
    """Assign sequential order_index (0..n-1) to items in their current list order. The single place
    that decides on-disk order_index values for a reorder/move — no gaps, no duplicates."""
    for index, item in enumerate(items):
        item.order_index = index


def _apply_engagement_form(engagement: Engagement, form, db) -> None:
    """Shared field-setting for the edit route (port of lotek's ``routes/engagements.py::
    _apply_engagement_form``). Unlike ``engagement_new`` above -- which never touches ``status`` and
    leaves it at the model default ("in_progress") -- this ALSO sets ``status``, since edit is the first
    place a user can change it after creation.

    Client resolution mirrors ``engagement_new``'s select-existing-or-create-by-name convention (rather
    than lotek's simpler client_id-only parse), so editing behaves the same way creating does.
    """
    engagement.name = (form.get("name") or "").strip()
    engagement.scope_type = (form.get("scope_type") or "external").strip() or "external"
    engagement.company_name = (form.get("company_name") or "").strip() or None
    engagement.status = (form.get("status") or "in_progress").strip() or "in_progress"
    engagement.start_date = _parse_date(form.get("start_date"))
    engagement.end_date = _parse_date(form.get("end_date"))

    ClientModel = client_model()
    client = None
    client_id = _as_id(form.get("client_id"))
    new_client_name = (form.get("new_client_name") or "").strip()
    if client_id is not None:
        client = db.get(ClientModel, client_id)
    if client is None and new_client_name:
        client = db.scalar(select(ClientModel).where(ClientModel.name == new_client_name))
        if client is None:
            client = ClientModel(name=new_client_name)
            db.add(client)
            db.flush()
    engagement.client_id = client.id if client is not None else None


def register(api_bp, bp) -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # =============================================================================== UI: engagements

    @bp.get("/engagements", endpoint="engagements")
    def engagements():
        with open_session() as db:
            rows = db.scalars(select(Engagement).order_by(Engagement.created_at.desc())).all()
            return render_template(
                "scribble/engagements.html", engagements=rows, client_names=client_names(db, rows)
            )

    @bp.route("/engagements/new", methods=["GET", "POST"], endpoint="engagement_new")
    def engagement_new():
        ClientModel = client_model()
        with open_session() as db:
            if request.method == "POST":
                name = (request.form.get("name") or "").strip()
                if not name:
                    clients = db.scalars(select(ClientModel).order_by(ClientModel.name)).all()
                    return (
                        render_template(
                            "scribble/engagement_new.html",
                            clients=clients,
                            error="Name is required.",
                        ),
                        400,
                    )

                client = None
                client_id = _as_id(request.form.get("client_id"))
                new_client_name = (request.form.get("new_client_name") or "").strip()
                if client_id is not None:
                    client = db.get(ClientModel, client_id)
                if client is None and new_client_name:
                    client = db.scalar(select(ClientModel).where(ClientModel.name == new_client_name))
                    if client is None:
                        client = ClientModel(name=new_client_name)
                        db.add(client)
                        db.flush()

                engagement = Engagement(
                    name=name,
                    client_id=(client.id if client is not None else None),
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

            clients = db.scalars(select(ClientModel).order_by(ClientModel.name)).all()
            return render_template("scribble/engagement_new.html", clients=clients, error=None)

    # =============================================================================== UI: edit / delete

    @bp.get("/engagements/<int:engagement_id>/edit", endpoint="engagement_edit_page")
    def engagement_edit_page(engagement_id: int):
        ClientModel = client_model()
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            clients = db.scalars(select(ClientModel).order_by(ClientModel.name)).all()
            return render_template(
                "scribble/engagement_edit.html", engagement=engagement, clients=clients, error=None
            )

    @bp.post("/engagements/<int:engagement_id>/edit", endpoint="engagement_edit")
    def engagement_edit(engagement_id: int):
        ClientModel = client_model()
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            name = (request.form.get("name") or "").strip()
            if not name:
                clients = db.scalars(select(ClientModel).order_by(ClientModel.name)).all()
                return (
                    render_template(
                        "scribble/engagement_edit.html",
                        engagement=engagement,
                        clients=clients,
                        error="Name is required.",
                    ),
                    400,
                )
            _apply_engagement_form(engagement, request.form, db)
            db.commit()
        return redirect(url_for("scribble.engagements"))

    @bp.post("/engagements/<int:engagement_id>/delete", endpoint="engagement_delete")
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
            storage_paths = [a.storage_path for a in engagement.artifacts]
            db.delete(engagement)  # cascades to groups/findings/artifacts/variable_values (delete-orphan)
            db.commit()
        for storage_path in storage_paths:
            delete_file(cfg, storage_path)
        return redirect(url_for("scribble.engagements"))

    # =============================================================================== UI: board (detail)

    @bp.get("/engagements/<int:engagement_id>", endpoint="engagement_board")
    def engagement_board(engagement_id: int):
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)

            groups = sorted(engagement.groups, key=lambda g: g.order_index)
            board_groups = [
                {"group": g, "findings": _display_order(g.findings, g.order_mode)} for g in groups
            ]
            ungrouped = sorted(
                (f for f in engagement.findings if f.group_id is None),
                key=lambda f: (severity_rank(f.severity), f.order_index),
            )

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

            return render_template(
                "scribble/engagement.html",
                engagement=engagement,
                client_name=(client.name if client is not None else None),
                board_groups=board_groups,
                ungrouped=ungrouped,
                templates=templates,
                assessment_types=assessment_types,
            )

    # =============================================================================== UI: groups

    @bp.post("/engagements/<int:engagement_id>/groups", endpoint="create_group")
    def create_group(engagement_id: int):
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            name = (request.form.get("name") or "").strip()
            if name:
                assessment_type_id = _as_int(request.form.get("assessment_type_id"))
                at = db.get(AssessmentType, assessment_type_id) if assessment_type_id is not None else None
                group = FindingGroup(
                    engagement_id=engagement_id,
                    assessment_type=at,
                    name=name,
                    order_index=len(engagement.groups),
                )
                db.add(group)
                db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    @bp.post("/engagements/<int:engagement_id>/groups/<int:group_id>/delete", endpoint="delete_group")
    def delete_group(engagement_id: int, group_id: int):
        with open_session() as db:
            group = db.get(FindingGroup, group_id)
            if group is None or group.engagement_id != engagement_id:
                abort(404)
            # Detach child findings rather than deleting them: removing a report *section* must never
            # silently destroy authored findings (PLAN.md's board is a two-level tree, not a hierarchy
            # of ownership over content).
            for finding in list(group.findings):
                finding.group_id = None
            db.delete(group)
            db.commit()
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    # =============================================================================== UI: add finding

    @bp.post("/engagements/<int:engagement_id>/findings", endpoint="add_finding")
    def add_finding(engagement_id: int):
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)

            template_id = _as_int(request.form.get("template_id"))
            template = db.get(VulnerabilityTemplate, template_id) if template_id is not None else None
            if template is not None:
                group_id = _as_int(request.form.get("group_id"))
                group = db.get(FindingGroup, group_id) if group_id is not None else None
                if group is not None and group.engagement_id != engagement_id:
                    group = None  # defensive: never attach to another engagement's group

                siblings = (
                    group.findings
                    if group is not None
                    else [f for f in engagement.findings if f.group_id is None]
                )
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
        "/engagements/<int:engagement_id>/findings/<int:finding_id>/delete", endpoint="delete_finding"
    )
    def delete_finding(engagement_id: int, finding_id: int):
        cfg = get_config()
        with open_session() as db:
            finding = db.get(EngagementFinding, finding_id)
            if finding is None or finding.engagement_id != engagement_id:
                abort(404)
            # Unlike delete_group (which deliberately only detaches its children -- a report *section*
            # isn't the same thing as the findings inside it), a finding IS its content: deleting it
            # must take its evidence with it. EngagementFinding.artifacts has no delete/delete-orphan
            # cascade (models.py) and Artifact.finding_id is nullable, so without this an ORM-level
            # `db.delete(finding)` would silently NULL out each artifact's finding_id (orphaning it)
            # rather than removing it. Delete the rows explicitly, then best-effort clean their on-disk
            # files (mirrors artifacts_api.delete_artifact).
            storage_paths = [a.storage_path for a in finding.artifacts]
            for artifact in list(finding.artifacts):
                db.delete(artifact)
            db.delete(finding)
            db.commit()
        for storage_path in storage_paths:
            delete_file(cfg, storage_path)
        return redirect(url_for("scribble.engagement_board", engagement_id=engagement_id))

    # =============================================================================== UI: finding detail

    @bp.route("/findings/<int:finding_id>", methods=["GET", "POST"], endpoint="finding_detail")
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

    @api_bp.post("/engagements/<int:engagement_id>/groups/reorder")
    def reorder_groups(engagement_id: int):
        payload = request.get_json(silent=True) or {}
        order = payload.get("order")
        if not isinstance(order, list):
            return jsonify(error="order must be a list of group ids"), 400

        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                return jsonify(error="engagement not found"), 404

            existing = {g.id: g for g in engagement.groups}
            ordered_ids: list[int] = []
            seen: set[int] = set()
            for raw_id in order:
                gid = _as_int(raw_id)
                if gid is not None and gid in existing and gid not in seen:
                    ordered_ids.append(gid)
                    seen.add(gid)

            # Anything belonging to this engagement that the client didn't mention (a stale/partial
            # payload, e.g. a group created concurrently in another tab) keeps its relative order and
            # is appended after the ones explicitly placed, so no group is ever silently dropped or
            # left with an undefined position.
            leftover = sorted((g for g in existing.values() if g.id not in seen), key=lambda g: g.order_index)
            ordered_ids.extend(g.id for g in leftover)

            for index, gid in enumerate(ordered_ids):
                existing[gid].order_index = index
            db.commit()

            result = [{"id": gid, "order_index": index} for index, gid in enumerate(ordered_ids)]
        return jsonify(ok=True, order=result)

    # =============================================================================== API: move finding

    @api_bp.post("/findings/<int:finding_id>/move")
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
                target_group_id = _as_int(raw_group_id)
                if target_group_id is None:
                    return jsonify(error="group_id must be an integer or null"), 400
                target_group = db.get(FindingGroup, target_group_id)
                if target_group is None or target_group.engagement_id != finding.engagement_id:
                    return jsonify(error=f"group {target_group_id} not found on this engagement"), 404

            old_group = finding.group
            old_group_id = old_group.id if old_group is not None else None
            new_group_id = target_group.id if target_group is not None else None
            same_group = old_group_id == new_group_id

            # Compute both affected lists BEFORE mutating `finding` — reads reflect the pre-move state
            # unambiguously, independent of session autoflush timing.
            #
            # The client's `order_index` in the payload is a slot in the RENDERED DOM order (board.js
            # reads DOM position), and for an ``auto_severity`` group the board renders severity-ranked,
            # NOT order_index-ranked (see ``_display_order`` and the board template). So dest_siblings
            # must be arranged the SAME way the board displayed them before inserting at the client
            # index — otherwise the finding lands among the wrong neighbours, and the _reindex + manual
            # flip below would freeze an order the user never chose (board order != document order). The
            # ungrouped bucket has no manual mode and is always shown severity-first, so it uses the same
            # severity-then-order_index key the board view uses for it.
            if target_group is not None:
                dest_siblings = _display_order(
                    [f for f in target_group.findings if f.id != finding.id], target_group.order_mode
                )
            else:
                dest_siblings = sorted(
                    (f for f in finding.engagement.findings if f.group_id is None and f.id != finding.id),
                    key=lambda f: (severity_rank(f.severity), f.order_index),
                )
            index = max(0, min(requested_index, len(dest_siblings)))
            dest_siblings.insert(index, finding)

            remaining = None
            if not same_group and old_group is not None:
                remaining = sorted(
                    (f for f in old_group.findings if f.id != finding.id), key=lambda f: f.order_index
                )

            _reindex(dest_siblings)
            if remaining is not None:
                _reindex(remaining)

            finding.group_id = new_group_id

            # The first manual drag flips the destination group to manual; "re-rank by severity"
            # (POST /groups/<id> {order_mode: "auto_severity"}) is the explicit way back
            # (PLAN.md §4 "Grouping & ordering UX"). Any drag into a group is a deliberate manual
            # placement, so this is unconditional, not just-once.
            if target_group is not None:
                target_group.order_mode = OrderMode.manual

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
                    {"id": old_group.id, "order_mode": old_group.order_mode.value}
                    if (old_group is not None and not same_group)
                    else None
                ),
            }
        return jsonify(response)

    # =============================================================================== API: update group

    @api_bp.post("/groups/<int:group_id>")
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
