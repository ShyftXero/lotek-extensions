"""Autosave API for rich-text content blocks (WS4 Phase A).

Canonical storage is ProseMirror JSON, one doc per named block, in ``EngagementFinding.content_json``
(a dict keyed by block name — see ``scribble/content/schema.py``). This module adds the block
read/write endpoints on top of that: the client (``scribble/static/editor.js``) debounces edits and
PUTs/POSTs the current doc; the server validates it's a ProseMirror ``doc``, stores it, and re-derives +
caches sanitized HTML via ``scribble.content.render_html.render_block`` into ``content_html`` so list
views/previews never need to walk JSON themselves.

This module does **not** import or edit ``scribble/api.py`` / ``scribble/blueprint.py`` /
``scribble/__init__.py`` (frozen; see ``plans/CONTRACTS.md`` ownership map). Instead it exposes
``register(api_bp, bp)``, which the driver calls (from the mount path, before the blueprints are
registered on the host app) to attach these routes onto the shared ``scribble_api`` blueprint object.
``tests/test_editor.py`` calls it the same way against a throwaway blueprint pair.

Note on ``content_html`` semantics: the cached HTML here is the **unresolved editor/preview render**
(``{{VARIABLE}}`` nodes render as literal ``{{KEY}}`` chips, inline images render via a best-effort
artifact URL guess — see ``_artifact_url`` below). Resolving variables against a real engagement context
for the final report is WS7's job (``reporting/context.py`` calls ``templating/resolver.py``'s
``resolve_doc``/``make_var_resolver`` and re-renders through the same ``render_block`` walker). Keeping
the two renders separate means this endpoint never needs engagement/template context to do its job.
"""

from __future__ import annotations

from flask import jsonify, request

from scribble.content import schema
from scribble.content.render_html import render_block
from scribble.deps import get_config, open_session
from scribble.models import EngagementFinding

_REGISTERED = False


def register(api_bp, bp) -> None:
    """Attach the block autosave routes onto the shared API blueprint.

    Also registers a small context processor on ``bp`` (the UI blueprint) exposing
    ``scribble_api_prefix`` and ``scribble_variable_keys`` to templates, so
    ``scribble/templates/scribble/_editor.html`` can build API URLs and offer a built-in variable list
    without hardcoding the mount prefix. Idempotent: safe to call more than once per process (each
    fresh Flask app built in tests re-runs the *existing* deferred routes via
    ``app.register_blueprint``; we must not append duplicates to the blueprint's route table).
    """
    global _REGISTERED
    if _REGISTERED:
        return

    @api_bp.post("/findings/<int:finding_id>/blocks/<string:block>")
    def autosave_block(finding_id: int, block: str):
        doc = request.get_json(silent=True)
        if not schema.is_doc(doc):
            return (
                jsonify(ok=False, error="body must be a ProseMirror doc: {'type': 'doc', 'content': [...]}"),
                400,
            )

        with open_session() as db:
            finding = db.get(EngagementFinding, finding_id)
            if finding is None:
                return jsonify(ok=False, error="finding not found"), 404

            html = render_block(doc, artifact_url=_artifact_url)

            # Reassign (don't mutate in place): content_json/content_html are plain JSON columns, not
            # MutableDict, so SQLAlchemy only detects a new object being set on the attribute.
            content_json = dict(finding.content_json or {})
            content_json[block] = doc
            finding.content_json = content_json

            content_html = dict(finding.content_html or {})
            content_html[block] = html
            finding.content_html = content_html

            db.commit()
            return jsonify(ok=True, html=html)

    @api_bp.get("/findings/<int:finding_id>/blocks/<string:block>")
    def get_block(finding_id: int, block: str):
        with open_session() as db:
            finding = db.get(EngagementFinding, finding_id)
            if finding is None:
                return jsonify(ok=False, error="finding not found"), 404

            doc = (finding.content_json or {}).get(block) or schema.empty_doc()
            html = (finding.content_html or {}).get(block, "")
            return jsonify(ok=True, doc=doc, html=html)

    @bp.context_processor
    def _inject_editor_globals():
        try:
            cfg = get_config()
            api_prefix = f"{cfg.url_prefix}/api"
        except RuntimeError:  # pragma: no cover - defensive; get_config requires an app context
            api_prefix = "/scribble/api"
        from scribble.templating.resolver import BUILTIN_KEYS

        return {"scribble_api_prefix": api_prefix, "scribble_variable_keys": list(BUILTIN_KEYS)}

    _REGISTERED = True


def _artifact_url(artifact_id: int) -> str:
    """Best-effort artifact URL for the editor-preview render.

    WS5 owns the actual artifact serve route (see ``plans/CONTRACTS.md`` §8); it hadn't landed at the
    time this workstream was built. This guess (``<prefix>/api/artifacts/<id>/raw``) keeps
    ``render_block`` callable end-to-end today. **Follow-up for the driver:** confirm/align this path
    once WS5's upload/serve endpoint is merged, or inject the real URL builder here.
    """
    try:
        prefix = get_config().url_prefix
    except RuntimeError:  # pragma: no cover - defensive; no app context
        prefix = "/scribble"
    return f"{prefix}/api/artifacts/{artifact_id}/raw"
