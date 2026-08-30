"""Vulnerability template LIBRARY UI + API (WS2 owns this module).

Full CRUD for the vuln template library, keyed on **edit-in-place**:

- List / search / filter templates by category / severity / tag (`bp`, HTML).
- Template detail/edit page (`bp`, HTML) — metadata form (name, category, default_severity,
  cvss_score/vector, references, tags) + one rich-text surface per content block (description/
  remediation/details). **Save writes back to the SAME `VulnerabilityTemplate` row** (edit-in-place):
  the metadata form and the block editors both POST JSON to the `api_bp` endpoints below and never
  create a new row.
- Create (`bp` page + `api_bp` JSON action).
- **Duplicate** (`api_bp` JSON action) — a SEPARATE, optional action that forks a template into a new
  row (``name + " (copy)"``), copying every field (including the original's `active` flag); the
  `content_html` cache is rebuilt from the copied `content_json` rather than copied.
- Delete / deactivate (`api_bp` JSON action) — never a hard delete: the `/delete` endpoint *toggles*
  `active`, so it is fully reversible (the detail page's state-aware button reactivates through it).
- Tags: assign existing or create-and-assign (by name; unknown names create a new `Tag` row).

Content-block editing note: `scribble/templates/scribble/_editor.html` + `scribble/static/editor.js`
(WS4) hardcode their autosave URL to ``.../findings/<finding_id>/blocks/<block>`` and read/write an
``EngagementFinding`` row. Templates are a different table with an independently-numbered primary key,
so pointing that partial at a template id would silently misfire: it would try to autosave against
`EngagementFinding` id `<template.id>`, which either 404s or — worse — if a finding happens to share
that numeric id, clobbers *its* content. Per the WS2 brief we do not edit WS4's files, so instead this
module exposes its own template-scoped block endpoints (mirroring `scribble/autosave_api.py`'s shape:
same doc validation and `render_block`-and-cache-`content_html` behavior, but intentionally omitting
artifact-URL resolution since library templates predate any engagement/artifacts) and the detail page
wires a small bespoke editor surface (see `library_detail.html`) that reuses `editor.js`'s *exported*
`window.ScribbleEditor._internal.{docToFragment,domToDoc}` JSON<->DOM converters (explicitly documented
in that file as a reuse point for exactly this kind of adapter) instead of `ScribbleEditor.mount()`.

Routes:
    UI (``bp``):
        GET  /library                          list + search/filter (?q=&category=&severity=&tag=&inactive=1)
        GET  /library/new                      create form
        GET  /library/<id>                     detail/edit-in-place page
    JSON (``api_bp``):
        POST /templates                        create
        POST /templates/<id>                   update in place (edit-in-place; id never changes)
        POST /templates/<id>/duplicate          fork into a new row
        POST /templates/<id>/delete             deactivate (active=False)
        GET  /templates/<id>/blocks/<block>      read one content block
        POST /templates/<id>/blocks/<block>      write + re-render/cache one content block

Contract: expose `def register(api_bp, bp) -> None` (idempotent) that adds routes to `bp` (UI) and
`api_bp` (JSON). Keep the `library` endpoint name. Wired in `scribble/__init__.py`.
"""

from __future__ import annotations

from typing import Any

from flask import abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy import select

from scribble.artifacts_api import _as_uuid  # one shared body-id parser (lotek#335)
from scribble.content import schema
from scribble.content.render_html import render_block
from scribble.deps import current_actor_username, host_can_write, open_session, severity_enum
from scribble.enums import Severity
from scribble.models import ScribbleVulnMap, Tag, VulnerabilityTemplate

_REGISTERED = False


def register(api_bp, bp) -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # ------------------------------------------------------------------------------- UI (bp)

    @bp.get("/library", endpoint="library")
    def library():
        q = (request.args.get("q") or "").strip()
        category = (request.args.get("category") or "").strip()
        severity_raw = (request.args.get("severity") or "").strip()
        tag = (request.args.get("tag") or "").strip()
        show_inactive = request.args.get("inactive") == "1"

        with open_session() as db:
            stmt = select(VulnerabilityTemplate)
            if not show_inactive:
                stmt = stmt.where(VulnerabilityTemplate.active.is_(True))
            if q:
                stmt = stmt.where(VulnerabilityTemplate.name.ilike(f"%{q}%"))
            if category:
                stmt = stmt.where(VulnerabilityTemplate.category == category)
            severity = _parse_severity(severity_raw) if severity_raw else None
            if severity is not None:
                stmt = stmt.where(VulnerabilityTemplate.default_severity == severity)
            if tag:
                stmt = stmt.join(VulnerabilityTemplate.tags).where(Tag.name == tag)
            stmt = stmt.order_by(VulnerabilityTemplate.name)
            templates = db.scalars(stmt).unique().all()

            categories = sorted(
                {
                    c
                    for (c,) in db.execute(
                        select(VulnerabilityTemplate.category).where(
                            VulnerabilityTemplate.category.isnot(None)
                        )
                    ).all()
                    if c
                }
            )
            all_tags = db.scalars(select(Tag).order_by(Tag.name)).all()

            return render_template(
                "scribble/library.html",
                templates=templates,
                q=q,
                category=category,
                severity=severity_raw,
                tag=tag,
                show_inactive=show_inactive,
                categories=categories,
                all_tags=all_tags,
                severities=list(severity_enum()),
            )

    @bp.get("/library/new", endpoint="library_new")
    def library_new():
        with open_session() as db:
            all_tags = db.scalars(select(Tag).order_by(Tag.name)).all()
        return render_template(
            "scribble/library_new.html",
            severities=list(severity_enum()),
            all_tags=all_tags,
        )

    @bp.get("/library/<uuid:template_id>", endpoint="library_detail")
    def library_detail(template_id: int):
        with open_session() as db:
            template = db.get(VulnerabilityTemplate, template_id)
            if template is None:
                abort(404)
            all_tags = db.scalars(select(Tag).order_by(Tag.name)).all()
            tags_csv = ", ".join(t.name for t in template.tags)
            references_text = "\n".join(template.references or [])
            content_json = template.content_json or {}
            block_docs = {
                block: (
                    content_json.get(block)
                    if schema.is_doc(content_json.get(block))
                    else schema.empty_doc()
                )
                for block in schema.DEFAULT_BLOCKS
            }
            return render_template(
                "scribble/library_detail.html",
                template=template,
                blocks=schema.DEFAULT_BLOCKS,
                block_docs=block_docs,
                severities=list(severity_enum()),
                tags_csv=tags_csv,
                references_text=references_text,
                all_tags=all_tags,
            )

    # ----------------------------------------------------------------- vuln-map (ext#142)
    # The title/source -> template mapping `promote_job` resolves through. Managed here in the library
    # area (a library-wide, tenant-free table like the templates it points at). No-JS form POSTs.

    @bp.get("/library/vuln-map", endpoint="vuln_map")
    def vuln_map_page():
        with open_session() as db:
            templates = db.scalars(
                select(VulnerabilityTemplate)
                .where(VulnerabilityTemplate.active.is_(True))
                .order_by(VulnerabilityTemplate.name)
            ).all()
            template_names = {t.id: t.name for t in templates}
            mappings = [
                {"id": m.id, "source": m.source, "title_pattern": m.title_pattern,
                 "dedupe_prefix": m.dedupe_prefix, "template_id": m.template_id,
                 "template_name": template_names.get(m.template_id, "(missing template)")}
                for m in db.scalars(select(ScribbleVulnMap).order_by(ScribbleVulnMap.id)).all()
            ]
            return render_template(
                "scribble/vuln_map.html", mappings=mappings, templates=templates,
                notice=request.args.get("notice"), error=request.args.get("error"),
            )

    @bp.post("/library/vuln-map", endpoint="vuln_map_create")
    def vuln_map_create():
        if not host_can_write():
            abort(403)
        source = (request.form.get("source") or "").strip() or None
        title_pattern = (request.form.get("title_pattern") or "").strip() or None
        dedupe_prefix = (request.form.get("dedupe_prefix") or "").strip() or None
        template_id = _as_uuid(request.form.get("template_id"))
        if template_id is None:
            return redirect(url_for("scribble.vuln_map", error="A template is required."))
        if not (source or title_pattern or dedupe_prefix):
            return redirect(url_for(
                "scribble.vuln_map",
                error="At least one match key (source, title pattern, or dedupe prefix) is required."))
        with open_session() as db:
            t = db.get(VulnerabilityTemplate, template_id)
            if t is None or not t.active:
                return redirect(url_for("scribble.vuln_map", error="That template no longer exists."))
            db.add(ScribbleVulnMap(
                source=source, title_pattern=title_pattern, dedupe_prefix=dedupe_prefix,
                template_id=template_id, created_by=current_actor_username(),
            ))
            db.commit()
        return redirect(url_for("scribble.vuln_map", notice="Mapping added."))

    @bp.post("/library/vuln-map/<uuid:map_id>/delete", endpoint="vuln_map_delete")
    def vuln_map_delete(map_id):
        if not host_can_write():
            abort(403)
        with open_session() as db:
            m = db.get(ScribbleVulnMap, map_id)
            if m is not None:
                db.delete(m)
                db.commit()
        return redirect(url_for("scribble.vuln_map", notice="Mapping deleted."))

    # ------------------------------------------------------------------------------- JSON (api_bp)

    @api_bp.post("/templates")
    def create_template():
        payload = request.get_json(silent=True) or {}
        name = (payload.get("name") or "").strip()
        if not name:
            return jsonify(ok=False, error="name is required"), 400

        raw_sev = payload.get("default_severity")
        if raw_sev:
            severity = _parse_severity(raw_sev)
            if severity is None:
                return jsonify(ok=False, error=f"invalid severity {raw_sev!r}"), 400
        else:
            severity = severity_enum().medium

        with open_session() as db:
            template = VulnerabilityTemplate(
                name=name,
                category=(payload.get("category") or None) or None,
                default_severity=severity,
                cvss_score=_parse_float(payload.get("cvss_score")),
                cvss_vector=(payload.get("cvss_vector") or None) or None,
                references=_parse_references(payload.get("references")),
                content_json={},
                content_html={},
                active=True,
            )
            if "tags" in payload:
                _apply_tags(db, template, payload.get("tags"))
            db.add(template)
            db.commit()
            return (
                jsonify(
                    ok=True,
                    id=template.id,
                    redirect=url_for("scribble.library_detail", template_id=template.id),
                ),
                201,
            )

    @api_bp.post("/templates/<uuid:template_id>")
    def update_template(template_id: int):
        payload = request.get_json(silent=True) or {}
        with open_session() as db:
            template = db.get(VulnerabilityTemplate, template_id)
            if template is None:
                return jsonify(ok=False, error="template not found"), 404

            if "name" in payload:
                name = (payload.get("name") or "").strip()
                if not name:
                    return jsonify(ok=False, error="name cannot be empty"), 400
                template.name = name
            if "category" in payload:
                template.category = (payload.get("category") or None) or None
            if "default_severity" in payload:
                raw_sev = payload.get("default_severity")
                severity = _parse_severity(raw_sev)
                if severity is None:
                    return jsonify(ok=False, error=f"invalid severity {raw_sev!r}"), 400
                template.default_severity = severity
            if "cvss_score" in payload:
                template.cvss_score = _parse_float(payload.get("cvss_score"))
            if "cvss_vector" in payload:
                template.cvss_vector = (payload.get("cvss_vector") or None) or None
            if "references" in payload:
                template.references = _parse_references(payload.get("references"))
            if "active" in payload:
                template.active = bool(payload.get("active"))
            if "tags" in payload:
                _apply_tags(db, template, payload.get("tags"))

            # Edit-in-place also re-derives + caches content_html per block, so the list/preview
            # surfaces never go stale relative to content_json (mirrors autosave_api's per-block
            # behavior for the metadata-only save path too).
            _recache_content_html(template)

            db.commit()
            return jsonify(ok=True, id=template.id)

    @api_bp.post("/templates/<uuid:template_id>/duplicate")
    def duplicate_template(template_id: int):
        with open_session() as db:
            original = db.get(VulnerabilityTemplate, template_id)
            if original is None:
                return jsonify(ok=False, error="template not found"), 404

            dup = VulnerabilityTemplate(
                name=f"{original.name} (copy)",
                category=original.category,
                default_severity=original.default_severity,
                cvss_score=original.cvss_score,
                cvss_vector=original.cvss_vector,
                content_json=_deep_copy(original.content_json or {}),
                content_html={},
                references=list(original.references or []),
                active=original.active,
                tags=list(original.tags),
            )
            _recache_content_html(dup)
            db.add(dup)
            db.commit()
            return (
                jsonify(
                    ok=True,
                    id=dup.id,
                    redirect=url_for("scribble.library_detail", template_id=dup.id),
                ),
                201,
            )

    @api_bp.post("/templates/<uuid:template_id>/delete")
    def delete_template(template_id: int):
        """Toggle a template's active flag (soft delete / reactivate) — reversible.

        Named ``/delete`` for the primary deactivate action, but toggles so the detail page's
        state-aware "Deactivate"/"Reactivate" button does the right thing in both directions.
        """
        with open_session() as db:
            template = db.get(VulnerabilityTemplate, template_id)
            if template is None:
                return jsonify(ok=False, error="template not found"), 404
            template.active = not template.active
            db.commit()
            return jsonify(ok=True, active=template.active)

    @api_bp.post("/templates/<uuid:template_id>/blocks/<string:block>")
    def save_template_block(template_id: int, block: str):
        doc = request.get_json(silent=True)
        if not schema.is_doc(doc):
            return (
                jsonify(
                    ok=False,
                    error="body must be a ProseMirror doc: {'type': 'doc', 'content': [...]}",
                ),
                400,
            )
        with open_session() as db:
            template = db.get(VulnerabilityTemplate, template_id)
            if template is None:
                return jsonify(ok=False, error="template not found"), 404

            html = render_block(doc)

            # Reassign (don't mutate in place): content_json/content_html are plain JSON columns,
            # not MutableDict, so SQLAlchemy only detects a new object being set on the attribute.
            content_json = dict(template.content_json or {})
            content_json[block] = doc
            template.content_json = content_json

            content_html = dict(template.content_html or {})
            content_html[block] = html
            template.content_html = content_html

            db.commit()
            return jsonify(ok=True, html=html)

    @api_bp.get("/templates/<uuid:template_id>/blocks/<string:block>")
    def get_template_block(template_id: int, block: str):
        with open_session() as db:
            template = db.get(VulnerabilityTemplate, template_id)
            if template is None:
                return jsonify(ok=False, error="template not found"), 404
            doc = (template.content_json or {}).get(block) or schema.empty_doc()
            html = (template.content_html or {}).get(block, "")
            return jsonify(ok=True, doc=doc, html=html)


# --------------------------------------------------------------------------------------- helpers


def _parse_severity(value: Any) -> Severity | None:
    """Parse a raw severity string via the mounted ``severity_enum()`` (Lotek's when injected, else
    Scribble's own ``scribble.enums.Severity`` -- docs/LOTEK_ADOPTION.md §3.2). The return type hint
    stays ``Severity`` for documentation purposes; the two vocabularies are value-identical, so the
    actual returned object is whichever enum is mounted."""
    if value is None or value == "":
        return None
    try:
        return severity_enum()(value)
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_references(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    return []


def _apply_tags(db, template: VulnerabilityTemplate, value: Any) -> None:
    """Assign existing tags by name or create-and-assign unknown names."""
    if isinstance(value, list):
        names = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str):
        names = [v.strip() for v in value.split(",") if v.strip()]
    else:
        names = []

    seen: set[str] = set()
    ordered_names: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered_names.append(name)

    tags: list[Tag] = []
    for name in ordered_names:
        tag = db.scalar(select(Tag).where(Tag.name == name))
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
        tags.append(tag)
    template.tags = tags


def _recache_content_html(template: VulnerabilityTemplate) -> None:
    content_json = template.content_json or {}
    template.content_html = {
        block: render_block(doc) for block, doc in content_json.items() if schema.is_doc(doc)
    }


def _deep_copy(value: dict) -> dict:
    import copy as _copy

    return _copy.deepcopy(value)
