"""Autosave API for rich-text content blocks (WS4 Phase A).

Canonical storage is ProseMirror JSON, one doc per named block, in ``BoardFinding.content_json``
(a dict keyed by block name — see ``scribble/content/schema.py``). This module adds the block
read/write endpoints on top of that: the client (the kit's ``reporting-editor.js``) debounces edits and
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

from typing import Any

from flask import jsonify, request

from scribble.artifacts_api import _as_uuid  # one shared body-id parser (lotek#335)
from scribble.authz import can_view_engagement
from scribble.content import schema
from scribble.content.render_html import render_block
from scribble.deps import current_actor, get_config, open_session
from scribble.models import BoardFinding, ReportBoard

_REGISTERED = False


# --------------------------------------------------------------------------------------- one home
# The finding-block read/write/authorize, extracted so the legacy per-finding route (below) AND the
# unified autosave seam's ``finding`` kind (ext#321) call the SAME three functions. Two writers of "a
# ProseMirror doc into content_json + cache the rendered HTML" is exactly the drift this seam kills, so
# there is one implementation and both entry points delegate to it.


def save_finding_block(finding_id: Any, block: str, doc: Any) -> str | None:
    """Store ``doc`` into one named block of a finding + re-derive/cache its HTML; return the rendered
    HTML, or ``None`` if the finding does not exist (the legacy route maps that to 404; the seam gates it
    at ``authorize_finding`` first, so its ``save`` only ever runs on an existing finding).

    ``finding_id`` is accepted as either a ``uuid.UUID`` (the ``<uuid:...>`` route already parsed it) or a
    string (the seam carries a JSON string handle id) — ``_as_uuid`` coerces both, None for a non-id.
    """
    fid = _as_uuid(finding_id)
    if fid is None:
        return None
    with open_session() as db:
        finding = db.get(BoardFinding, fid)
        if finding is None:
            return None

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
        return html


def load_finding_block(finding_id: Any, block: str) -> tuple[Any, str] | None:
    """Return ``(doc, cached_html)`` for one block of a finding (an empty doc / "" for an unwritten
    block), or ``None`` if the finding does not exist. Same id-coercion contract as
    :func:`save_finding_block`."""
    fid = _as_uuid(finding_id)
    if fid is None:
        return None
    with open_session() as db:
        finding = db.get(BoardFinding, fid)
        if finding is None:
            return None
        doc = (finding.content_json or {}).get(block) or schema.empty_doc()
        html = (finding.content_html or {}).get(block, "")
        return doc, html


def authorize_finding(actor: Any, finding_id: Any) -> bool:
    """The mandatory CLIENT-TENANCY gate for the seam's ``finding`` kind. Fail-closed.

    The legacy ``/findings/<id>/blocks`` route is safe only because scribble's blueprint-wide
    ``before_request`` gate (``scribble.authz._gate``) resolves its ``<uuid:finding_id>`` view-arg to the
    finding's engagement and runs ``can_view_engagement`` (client tenancy — ``can_view_client``) before it
    runs. The seam is mounted by CORE on its OWN blueprint, and the finding id rides in the JSON BODY, not
    the view args, so that gate structurally cannot reach it — exactly the gap
    ``artifacts_api.create_artifact`` closes by calling ``can_view_engagement`` directly (authz.py).
    Without this, any logged-in actor could read/overwrite ANOTHER client's finding via
    ``/_kit/autosave``. So this reuses the SAME predicate (one home), not a copy.

    (Ownership — ``owner_id`` — is attribution only and never a gate; CLIENT tenancy is. The two are
    distinct axes; this gates the latter. Standalone scribble with no host mounted degrades to allow, like
    every other ``can_view_*`` caller.)

    The ``actor`` the seam hands us IS the host's current user (core mounts it as ``g.current_user``); we
    fall back to scribble's own ``current_actor()`` — the SAME injected hook — so the gate holds even if a
    future mounter passes nothing. No actor, a non-id, no such finding, or a foreign-tenant finding ->
    refuse (the seam answers 403; it does not distinguish those, the right fail-closed posture).
    """
    resolved = actor if actor is not None else current_actor()
    if resolved is None:
        return False
    fid = _as_uuid(finding_id)
    if fid is None:
        return False
    with open_session() as db:
        finding = db.get(BoardFinding, fid)
        if finding is None:
            return False
        engagement = db.get(ReportBoard, finding.engagement_id)
        return engagement is not None and can_view_engagement(engagement, resolved)


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

    # Both routes DELEGATE to the one-home functions above; the seam's ``finding`` kind calls the same
    # three, so there is a single writer of a finding block (ext#321).
    @api_bp.post("/findings/<uuid:finding_id>/blocks/<string:block>")
    def autosave_block(finding_id, block: str):
        doc = request.get_json(silent=True)
        if not schema.is_doc(doc):
            return (
                jsonify(ok=False, error="body must be a ProseMirror doc: {'type': 'doc', 'content': [...]}"),
                400,
            )
        html = save_finding_block(finding_id, block, doc)
        if html is None:
            return jsonify(ok=False, error="finding not found"), 404
        return jsonify(ok=True, html=html)

    @api_bp.get("/findings/<uuid:finding_id>/blocks/<string:block>")
    def get_block(finding_id, block: str):
        loaded = load_finding_block(finding_id, block)
        if loaded is None:
            return jsonify(ok=False, error="finding not found"), 404
        doc, html = loaded
        return jsonify(ok=True, doc=doc, html=html)

    # ext#321: register the finding surface on the unified autosave seam, so /_kit/autosave serves
    # findings through the SAME save/load/authorize as the legacy route (one home). Defensive import: a
    # kit pinned before the seam shipped has no register_autosave_kind — skip rather than break the mount.
    try:
        from lotek_kit.autosave import register_autosave_kind  # type: ignore[missing-import]
    except ImportError:  # pragma: no cover - only on an older pinned kit (or standalone scribble)
        pass
    else:
        register_autosave_kind(
            "finding",
            load=load_finding_block,
            save=save_finding_block,
            authorize=authorize_finding,
        )

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
