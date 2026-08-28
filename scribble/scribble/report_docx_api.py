"""WS8 UI route: the ``.docx`` report download.

Registered onto the existing UI blueprint via :func:`register`, kept in its own module (per
``plans/CONTRACTS.md`` ownership rules — WS8 does not edit ``blueprint.py``/``api.py``/``__init__.py``).
Whoever wires up routes (the driver, or ``scribble/__init__.py``) calls::

    from scribble.report_docx_api import register as register_report_docx
    register_report_docx(api_bp, bp)

One route:
- ``GET /engagements/<id>/report.docx`` — builds the frozen ``ReportContext`` and streams back a
  rendered, editable ``.docx`` attachment.

The route embeds a client's findings and evidence, so — like its ``/report`` HTML sibling in
``report_html_api.py`` — it is gated by the shared host-delegated
``scribble.authz.authorize_engagement_view`` before anything is built or streamed. See that module's
docstring for why the check lives in one place rather than being copied.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from flask import Response, abort

from scribble.artifacts_storage import artifact_bytes
from scribble.authz import authorize_engagement_view
from scribble.deps import open_session
from scribble.models import Engagement
from scribble.reporting.context import build_report_context
from scribble.reporting.render_docx import make_inline_artifact_url, render_report_docx

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Refuse to read an artifact larger than this into memory for embedding. Checked via ``stat`` before
# reading, so an oversized file is never even loaded (the render then degrades to caption-only for that
# artifact). 25 MiB is generous for a screenshot; ``reporting/render_docx.py`` /
# ``content/render_docx.py`` apply the same ceiling to any bytes they do receive.
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


def _artifact_url_factory(engagement: Engagement) -> Callable[[int], str]:
    """``artifact_url`` for ``build_report_context``: resolves inline-image content nodes to a
    placeholder baking in the artifact's ``storage_path`` (see ``render_docx.make_inline_artifact_url``,
    WS8's own copy of the WS7 placeholder trick)."""
    # Key by str(id): content_json's inlineImage ``artifactId`` is authored in the browser and arrives
    # as a JSON string (a UUID string since lotek#335; historically a JSON int), while ``a.id`` is a
    # ``uuid.UUID``. A plain dict keyed by the UUID would miss the string every time, silently dropping
    # every inline image from the report. str() on both sides normalises int/str/UUID uniformly.
    by_id = {str(a.id): a.storage_path for a in engagement.artifacts}

    def _url(artifact_id: int) -> str:
        return make_inline_artifact_url(by_id.get(str(artifact_id)) if artifact_id is not None else None)

    return _url


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").lower()
    return slug or "report"


def register(api_bp, bp) -> None:
    """Attach the docx report route to the UI blueprint ``bp``.

    ``api_bp`` is accepted to match the WS route-registration contract; the report is served as a
    binary attachment (not JSON), so no routes are added to the JSON API blueprint today.
    """
    if getattr(bp, "_ws8_docx_registered", False):
        return  # idempotent: register(app, ...) may be called more than once per process in tests
    bp._ws8_docx_registered = True  # type: ignore[attr-defined]

    @bp.get("/engagements/<uuid:engagement_id>/report.docx")
    def engagement_report_docx(engagement_id: int):
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            authorize_engagement_view(engagement)
            ctx = build_report_context(engagement, artifact_url=_artifact_url_factory(engagement))
            payload = render_report_docx(ctx, artifact_bytes=artifact_bytes)
            slug = _slugify(engagement.name)

        return Response(
            payload,
            mimetype=_DOCX_MIME,
            headers={"Content-Disposition": f'attachment; filename="{slug}-report.docx"'},
        )
