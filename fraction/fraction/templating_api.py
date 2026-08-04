"""Preview/lint HTTP endpoint for the templating engine (WS6).

Exposes :func:`register(api_bp, bp) -> None`, mirroring the per-workstream ``register(api_bp, bp)``
calling convention (each feature WS adds its routes to the two shared blueprints from ``fraction/api.py``
and ``fraction/blueprint.py`` without importing or editing those modules directly). WS6 owns
``fraction/templating/`` and this file only; per the file-ownership boundary in
``plans/CONTRACTS.md``, it does not import/edit ``api.py``, ``blueprint.py``, or ``__init__.py`` --
wiring ``register(api_bp, bp)`` into the live blueprint objects during app startup is left to the driver
(see ``plans/feat-ws6-templating.md`` "Remaining").

``POST /preview``
-----------------
Body (JSON): ``{"engagement_id": int, "finding_id": int | None, "text": str | None, "doc": dict | None}``

Exactly one of ``text`` / ``doc`` should be given (a raw string, or a ProseMirror doc). If both are
omitted, ``finding_id`` is required and every block of that finding's ``content_json`` is resolved
(the full :func:`fraction.templating.resolve_finding` preview).

Response: ``{"resolved": ..., "warnings": [str, ...]}``

- ``text`` mode: ``resolved`` is the resolved string (``resolve_text``).
- ``doc`` mode: ``resolved`` is the resolved+rendered sanitized HTML string for that one block
  (``resolve_doc`` + ``content.render_html.render_block``).
- neither given (``finding_id`` only): ``resolved`` is ``{block_name: resolved_html}`` for every block
  on the finding (``resolve_finding``).
- ``warnings`` is the sorted list of unknown ``{{KEY}}`` tokens referenced (``lint_text``/``lint_doc``,
  DB-aware via ``known_variable_keys`` so defined custom variables are not flagged as unknown).
"""

from __future__ import annotations

from flask import jsonify, request

from fraction.content.render_html import render_block
from fraction.deps import open_session
from fraction.models import Engagement, EngagementFinding
from fraction.templating import (
    build_full_context,
    known_variable_keys,
    lint_doc,
    lint_text,
    make_var_resolver,
    resolve_doc,
    resolve_finding,
    resolve_text,
)

# Tracks which Blueprint instances already had `/preview` added, so `register()` stays idempotent
# without monkey-patching an attribute onto the (frozen-contract) Blueprint object itself.
_REGISTERED: set[int] = set()


def register(api_bp, bp) -> None:
    """Attach the templating preview endpoint to the shared API blueprint.

    Only ``api_bp`` is used today -- ``POST /preview`` lands at ``<url_prefix>/api/preview`` once the
    driver mounts ``api_bp`` via ``fraction.register()``. ``bp`` (the UI blueprint) is accepted purely
    for calling-convention parity with other workstreams' ``*_api.py`` modules; WS6 has no page routes
    and does not use it. Idempotent: safe to call more than once on the same ``api_bp`` (e.g. once from
    test setup and once from the driver's real wiring) -- the route is only added the first time.
    """
    del bp  # unused: WS6 has no UI routes; kept for the shared register(api_bp, bp) convention.

    if id(api_bp) in _REGISTERED:
        return
    _REGISTERED.add(id(api_bp))

    @api_bp.post("/preview")
    def templating_preview():
        payload = request.get_json(silent=True) or {}
        engagement_id = payload.get("engagement_id")
        if not engagement_id:
            return jsonify(error="engagement_id is required"), 400

        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                return jsonify(error=f"engagement {engagement_id} not found"), 404

            finding = None
            finding_id = payload.get("finding_id")
            if finding_id is not None:
                finding = db.get(EngagementFinding, finding_id)
                if finding is None or finding.engagement_id != engagement.id:
                    return (
                        jsonify(error=f"finding {finding_id} not found on engagement {engagement_id}"),
                        404,
                    )

            known = known_variable_keys(db)
            text = payload.get("text")
            doc = payload.get("doc")

            if text is not None:
                ctx = build_full_context(db, engagement, finding)
                return jsonify(resolved=resolve_text(text, ctx), warnings=lint_text(text, known))

            if doc is not None:
                ctx = build_full_context(db, engagement, finding)
                resolve_var = make_var_resolver(ctx)
                html = render_block(resolve_doc(doc, ctx), resolve_var=resolve_var)
                return jsonify(resolved=html, warnings=lint_doc(doc, known))

            if finding is not None:
                warnings: set[str] = set()
                for block_doc in (finding.content_json or {}).values():
                    warnings.update(lint_doc(block_doc, known))
                return jsonify(resolved=resolve_finding(db, finding), warnings=sorted(warnings))

            return jsonify(error="one of text, doc, or finding_id is required"), 400
