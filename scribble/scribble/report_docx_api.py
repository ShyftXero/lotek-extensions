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
``report_html_api.py`` — it is gated by that module's host-delegated
``_authorize_engagement_view`` before anything is built or streamed. See CRIT-4's docstring there for
why the check lives in one place rather than being copied.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from flask import Response, abort

from scribble.deps import get_config, open_session
from scribble.models import Engagement
from scribble.report_html_api import _authorize_engagement_view
from scribble.reporting.context import build_report_context
from scribble.reporting.render_docx import make_inline_artifact_url, render_report_docx

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Refuse to read an artifact larger than this into memory for embedding. Checked via ``stat`` before
# reading, so an oversized file is never even loaded (the render then degrades to caption-only for that
# artifact). 25 MiB is generous for a screenshot; ``reporting/render_docx.py`` /
# ``content/render_docx.py`` apply the same ceiling to any bytes they do receive.
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


def _make_artifact_bytes(artifact_root: Path) -> Callable[[str], bytes | None]:
    """A ``storage_path -> bytes`` reader confined to ``artifact_root`` (safe_join-style guard) —
    mirrors ``report_html_api._make_artifact_bytes``; kept local since WS8 doesn't share files with
    WS7 and WS5's storage helpers aren't a frozen contract yet. Files over ``_MAX_ARTIFACT_BYTES`` are
    refused (returns ``None``) without being read into memory."""
    root = artifact_root.resolve()

    def _read(storage_path: str) -> bytes | None:
        if not storage_path:
            return None
        candidate = (root / storage_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None  # path would escape the artifact root — refuse
        if not candidate.is_file():
            return None
        try:
            if candidate.stat().st_size > _MAX_ARTIFACT_BYTES:
                return None  # oversized — don't read a huge blob into memory
            return candidate.read_bytes()
        except OSError:
            return None

    return _read


def _artifact_url_factory(engagement: Engagement) -> Callable[[int], str]:
    """``artifact_url`` for ``build_report_context``: resolves inline-image content nodes to a
    placeholder baking in the artifact's ``storage_path`` (see ``render_docx.make_inline_artifact_url``,
    WS8's own copy of the WS7 placeholder trick)."""
    by_id = {a.id: a.storage_path for a in engagement.artifacts}

    def _url(artifact_id: int) -> str:
        return make_inline_artifact_url(by_id.get(artifact_id))

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

    @bp.get("/engagements/<int:engagement_id>/report.docx")
    def engagement_report_docx(engagement_id: int):
        cfg = get_config()
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            _authorize_engagement_view(engagement)
            ctx = build_report_context(engagement, artifact_url=_artifact_url_factory(engagement))
            artifact_bytes = _make_artifact_bytes(cfg.artifact_root)
            payload = render_report_docx(ctx, artifact_bytes=artifact_bytes)
            slug = _slugify(engagement.name)

        return Response(
            payload,
            mimetype=_DOCX_MIME,
            headers={"Content-Disposition": f'attachment; filename="{slug}-report.docx"'},
        )
