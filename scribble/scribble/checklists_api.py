"""Engagement-checklist JSON API (browser surface, cookie-authed ``api_bp``).

Checklists are non-blocking visual reminders (plans/SCRIBBLE_CHECKLISTS.md). This module is the
functional core the UI drives: library CRUD (create / import / edit-in-place / hide / reset / duplicate /
export), assignment (0..N per engagement, copy-on-assign snapshot), and per-item status/note/finding
updates. All prose logic (markdown/JSON parse, rollup, snapshot) lives in ``scribble.checklists``.

Contract: ``register(api_bp, bp)`` (idempotent), mirroring the other feature modules. Mutations are POST
(codebase convention), not PATCH.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Response, jsonify, render_template, request
from sqlalchemy import select

from scribble import checklists as C
from scribble.artifacts_api import _as_uuid
from scribble.deps import open_session
from scribble.enums import ChecklistKind
from scribble.models import (
    ChecklistTemplate,
    ChecklistTemplateItem,
    Engagement,
    EngagementChecklist,
    EngagementChecklistItem,
    EngagementFinding,
)

_REGISTERED = False
_SEED_DIR = Path(__file__).parent / "seed" / "checklists"


def _kind(value: Any, default: ChecklistKind = ChecklistKind.coverage) -> ChecklistKind:
    try:
        return ChecklistKind(value)
    except (ValueError, TypeError):
        return default


def _template_out(t: ChecklistTemplate) -> dict[str, Any]:
    return {
        "id": t.id,
        "slug": t.slug,
        "name": t.name,
        "description": t.description,
        "kind": t.kind.value,
        "category": t.category,
        "builtin": t.builtin,
        "customized": bool(t.customized),
        "hidden": bool(t.hidden),
        "item_count": len(t.items),
    }


def _item_out(it: EngagementChecklistItem) -> dict[str, Any]:
    return {
        "id": it.id,
        "order_index": it.order_index,
        "section": it.section,
        "text": it.text,
        "guidance": it.guidance,
        "framework": it.framework,
        "control_ref": it.control_ref,
        "status": it.status,
        "bucket": C.status_bucket(it.status),
        "note": it.note,
        "finding_id": it.finding_id,
    }


def _checklist_out(ec: EngagementChecklist) -> dict[str, Any]:
    return {
        "id": ec.id,
        "template_id": ec.template_id,
        "name": ec.name,
        "kind": ec.kind.value,
        "include_in_report": ec.include_in_report,
        "order_index": ec.order_index,
        "recommended_status": C.RECOMMENDED_STATUS.get(ec.kind, []),
        "rollup": C.rollup(ec.items),
        "items": [_item_out(i) for i in ec.items],
    }


def _apply_template_dict(t: ChecklistTemplate, data: dict[str, Any]) -> None:
    """Overwrite ``t``'s fields + items from a normalized template dict (used by import/edit/reset)."""
    t.name = data["name"]
    t.description = data.get("description")
    t.kind = _kind(data.get("kind"))
    t.category = data.get("category")
    t.items.clear()
    for it in data["items"]:
        t.items.append(
            ChecklistTemplateItem(
                order_index=it["order_index"],
                section=it.get("section"),
                text=it["text"],
                guidance=it.get("guidance"),
                framework=it.get("framework"),
                control_ref=it.get("control_ref"),
                default_status=it.get("default_status"),
            )
        )


def register(api_bp, bp) -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # ----------------------------------------------------------------- UI (bp)

    @bp.get("/checklists", endpoint="checklists_library")
    def checklists_library():
        # The page is JS-driven off the JSON API below (list/create/import/edit/hide/reset/export).
        return render_template("scribble/checklists_library.html")

    # ----------------------------------------------------------------- library (templates)

    @api_bp.get("/checklists/templates")
    def list_checklist_templates():
        include_hidden = request.args.get("hidden") == "1"
        category = (request.args.get("category") or "").strip()
        with open_session() as db:
            stmt = select(ChecklistTemplate).where(ChecklistTemplate.active.is_(True))
            if not include_hidden:
                stmt = stmt.where(ChecklistTemplate.hidden.isnot(True))  # NULL-safe: NULL reads as not-hidden
            if category:
                stmt = stmt.where(ChecklistTemplate.category == category)
            rows = db.scalars(stmt.order_by(ChecklistTemplate.name)).all()
            return jsonify(ok=True, templates=[_template_out(t) for t in rows])

    @api_bp.get("/checklists/templates/suggest")
    def suggest_checklist_templates():
        # Suggest by assessment-type category, then everything else (so the dialog can group them).
        category = (request.args.get("category") or "").strip()
        with open_session() as db:
            rows = db.scalars(
                select(ChecklistTemplate)
                .where(ChecklistTemplate.active.is_(True), ChecklistTemplate.hidden.isnot(True))
                .order_by(ChecklistTemplate.name)
            ).all()
            suggested = [t for t in rows if category and t.category == category]
            others = [t for t in rows if t not in suggested]
            return jsonify(
                ok=True,
                suggested=[_template_out(t) for t in suggested],
                others=[_template_out(t) for t in others],
            )

    @api_bp.post("/checklists/templates")
    def create_checklist_template():
        payload = request.get_json(silent=True) or {}
        if payload.get("markdown"):
            data = C.parse_markdown(payload["markdown"])
            if payload.get("kind"):
                data["kind"] = payload["kind"]
            if payload.get("name"):
                data["name"] = payload["name"]
            if payload.get("category"):
                data["category"] = payload["category"]
        else:
            data = payload.get("template") or payload
        data = C.normalize_template_dict(data)
        if not data["items"] and not (payload.get("allow_empty")):
            return jsonify(ok=False, error="checklist has no items"), 400
        with open_session() as db:
            # Sanitize AND uniquify: never trust an imported slug raw (it becomes a DB key and a
            # Content-Disposition header value). _slugify strips to [a-z0-9-], clamps length, and
            # picks a non-colliding value, so an import can never inject a header or clobber a builtin.
            slug = _slugify(data.get("slug") or data["name"], db)
            t = ChecklistTemplate(
                slug=slug,
                name=data["name"],
                description=data.get("description"),
                kind=_kind(data.get("kind")),
                category=data.get("category"),
                builtin=False,
                customized=False,
                hidden=False,
                active=True,
            )
            _apply_template_dict(t, data)
            t.slug = slug
            db.add(t)
            db.commit()
            return jsonify(ok=True, template=_template_out(t)), 201

    @api_bp.post("/checklists/templates/<uuid:tid>")
    def edit_checklist_template(tid: int):
        payload = request.get_json(silent=True) or {}
        with open_session() as db:
            t = db.get(ChecklistTemplate, tid)
            if t is None:
                return jsonify(ok=False, error="not found"), 404
            # Metadata-only edits, or a full items replacement when "items" is present.
            if "name" in payload:
                t.name = (payload["name"] or t.name).strip()[:255]
            if "description" in payload:
                t.description = payload["description"] or None
            if "kind" in payload:
                t.kind = _kind(payload["kind"], t.kind)
            if "category" in payload:
                t.category = payload["category"] or None
            if "items" in payload:
                data = C.normalize_template_dict({**_template_full_dict(t), **payload})
                _apply_template_dict(t, data)
            if t.builtin:
                t.customized = True  # edit-in-place marks a builtin as diverged from shipped default
            db.commit()
            return jsonify(ok=True, template=_template_out(t))

    @api_bp.post("/checklists/templates/<uuid:tid>/hide")
    def hide_checklist_template(tid: int):
        payload = request.get_json(silent=True) or {}
        hidden = bool(payload.get("hidden", True))
        with open_session() as db:
            t = db.get(ChecklistTemplate, tid)
            if t is None:
                return jsonify(ok=False, error="not found"), 404
            t.hidden = hidden
            db.commit()
            return jsonify(ok=True, template=_template_out(t))

    @api_bp.post("/checklists/templates/<uuid:tid>/reset")
    def reset_checklist_template(tid: int):
        with open_session() as db:
            t = db.get(ChecklistTemplate, tid)
            if t is None:
                return jsonify(ok=False, error="not found"), 404
            if not t.builtin:
                return jsonify(ok=False, error="only builtins can be reset to default"), 400
            path = _SEED_DIR / f"{t.slug}.json"
            if not path.exists():
                return jsonify(ok=False, error="shipped default not found"), 404
            data = C.normalize_template_dict(json.loads(path.read_text()))
            _apply_template_dict(t, data)
            t.customized = False
            db.commit()
            return jsonify(ok=True, template=_template_out(t))

    @api_bp.post("/checklists/templates/<uuid:tid>/duplicate")
    def duplicate_checklist_template(tid: int):
        with open_session() as db:
            src = db.get(ChecklistTemplate, tid)
            if src is None:
                return jsonify(ok=False, error="not found"), 404
            data = C.normalize_template_dict(_template_full_dict(src))
            data["name"] = f"{src.name} (copy)"
            new = ChecklistTemplate(
                slug=_slugify(data["name"], db, force_unique=True),
                kind=src.kind,
                category=src.category,
                builtin=False,
                customized=False,
                hidden=False,
                active=True,
                name=data["name"],
                description=src.description,
            )
            _apply_template_dict(new, data)
            db.add(new)
            db.commit()
            return jsonify(ok=True, template=_template_out(new)), 201

    @api_bp.get("/checklists/templates/<uuid:tid>/export")
    def export_checklist_template(tid: int):
        fmt = (request.args.get("format") or "json").lower()
        with open_session() as db:
            t = db.get(ChecklistTemplate, tid)
            if t is None:
                return jsonify(ok=False, error="not found"), 404
            data = C.template_to_dict(t)
            if fmt in ("md", "markdown"):
                return Response(
                    C.to_markdown(data),
                    mimetype="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{t.slug}.md"'},
                )
            return Response(
                json.dumps(data, indent=2, ensure_ascii=False),
                mimetype="application/json",
                headers={"Content-Disposition": f'attachment; filename="{t.slug}.json"'},
            )

    # ----------------------------------------------------------------- assignment (engagement side)

    @api_bp.get("/engagements/<uuid:eid>/checklists")
    def list_engagement_checklists(eid: int):
        with open_session() as db:
            e = db.get(Engagement, eid)
            if e is None:
                return jsonify(ok=False, error="engagement not found"), 404
            rows = db.scalars(
                select(EngagementChecklist)
                .where(EngagementChecklist.engagement_id == eid)
                .order_by(EngagementChecklist.order_index)
            ).all()
            return jsonify(ok=True, checklists=[_checklist_out(ec) for ec in rows])

    @api_bp.post("/engagements/<uuid:eid>/checklists")
    def assign_engagement_checklist(eid: int):
        payload = request.get_json(silent=True) or {}
        try:
            template_id = _as_uuid(payload.get("template_id"))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="template_id must be an integer"), 400
        with open_session() as db:
            e = db.get(Engagement, eid)
            if e is None:
                return jsonify(ok=False, error="engagement not found"), 404
            t = db.get(ChecklistTemplate, template_id)
            if t is None:
                return jsonify(ok=False, error="template not found"), 404
            ec = C.assign_template(db, e, t, assigned_by=payload.get("assigned_by"))
            db.commit()
            return jsonify(ok=True, checklist=_checklist_out(ec)), 201

    @api_bp.post("/engagement-checklists/<uuid:cid>")
    def edit_engagement_checklist(cid: int):
        payload = request.get_json(silent=True) or {}
        with open_session() as db:
            ec = db.get(EngagementChecklist, cid)
            if ec is None:
                return jsonify(ok=False, error="not found"), 404
            if "include_in_report" in payload:
                ec.include_in_report = bool(payload["include_in_report"])
            if "name" in payload:
                ec.name = (payload["name"] or ec.name).strip()[:255]
            if "kind" in payload:
                ec.kind = _kind(payload["kind"], ec.kind)
            db.commit()
            return jsonify(ok=True, checklist=_checklist_out(ec))

    @api_bp.post("/engagement-checklists/<uuid:cid>/delete")
    def unassign_engagement_checklist(cid: int):
        with open_session() as db:
            ec = db.get(EngagementChecklist, cid)
            if ec is None:
                return jsonify(ok=False, error="not found"), 404
            db.delete(ec)
            db.commit()
            return jsonify(ok=True)

    @api_bp.post("/engagement-checklist-items/<uuid:iid>")
    def update_engagement_checklist_item(iid: int):
        payload = request.get_json(silent=True) or {}
        with open_session() as db:
            it = db.get(EngagementChecklistItem, iid)
            if it is None:
                return jsonify(ok=False, error="not found"), 404
            if "status" in payload:
                it.status = (payload["status"] or "").strip()[:64] or it.status
            if "note" in payload:
                it.note = payload["note"] or None
            if "finding_id" in payload:
                fid = payload["finding_id"]
                if fid is None:
                    it.finding_id = None
                else:
                    # A finding cross-link must reference a finding IN THIS engagement (no dangling or
                    # cross-engagement links). it.checklist.engagement_id is the item's engagement.
                    try:
                        fid = int(fid)
                    except (TypeError, ValueError):
                        return jsonify(ok=False, error="finding_id must be an integer or null"), 400
                    f = db.get(EngagementFinding, fid)
                    if f is None or f.engagement_id != it.checklist.engagement_id:
                        return jsonify(
                            ok=False, error="finding_id must reference a finding in this engagement"
                        ), 400
                    it.finding_id = fid
            if "updated_by" in payload:
                it.updated_by = payload["updated_by"]
            db.commit()
            return jsonify(ok=True, item=_item_out(it))


# --------------------------------------------------------------------------- helpers


def _template_full_dict(t: ChecklistTemplate) -> dict[str, Any]:
    d = C.template_to_dict(t)
    d["description"] = t.description
    return d


def _slugify(name: str, db, *, force_unique: bool = False) -> str:
    """A lowercase-dash slug from a name, unique against existing template slugs when needed."""
    import re

    base = re.sub(r"[^a-z0-9]+", "-", (name or "checklist").lower()).strip("-") or "checklist"
    slug = base[:120]
    if not force_unique and not db.scalar(
        select(ChecklistTemplate).where(ChecklistTemplate.slug == slug)
    ):
        return slug
    n = 2
    while db.scalar(select(ChecklistTemplate).where(ChecklistTemplate.slug == f"{base}-{n}")):
        n += 1
    return f"{base}-{n}"[:128]
