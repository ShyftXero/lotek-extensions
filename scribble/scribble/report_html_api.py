"""WS7 UI routes: the live HTML report + its self-contained/zip export.

Registered onto the existing UI blueprint via :func:`register`, kept in its own module (per
plans/CONTRACTS.md ownership rules — WS7 does not edit ``blueprint.py``/``api.py``/``__init__.py``).
Whoever wires up routes (the driver, or ``scribble/__init__.py`` in a later sprint) calls::

    from scribble.report_html_api import register as register_report_html
    register_report_html(api_bp, bp)

Two routes:
- ``GET /engagements/<id>/report``          — live render, assets embedded (self-contained page).
- ``GET /engagements/<id>/report/export``   — download; ``?format=html`` (default) or ``?format=zip``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from flask import Response, abort, request, url_for

from scribble.authz import authorize_engagement_view
from scribble.deps import get_config, open_session
from scribble.models import Engagement
from scribble.reporting.context import build_report_context
from scribble.reporting.render_html import export_zip, make_inline_artifact_url, render_report_html

# CRIT-4's tenancy predicate now lives in ``scribble/authz.py`` (it's also the primitive the
# blueprint-wide ``before_request`` gate uses — see that module's docstring for the full history).
# ``report_docx_api.py`` imports it from there directly, not from this module.


def _make_artifact_bytes(artifact_root: Path) -> Callable[[str], bytes | None]:
    """A ``storage_path -> bytes`` reader confined to ``artifact_root`` (safe_join-style guard)."""
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
            return candidate.read_bytes()
        except OSError:
            return None

    return _read


def _artifact_url_factory(engagement: Engagement) -> Callable[[int], str]:
    """``artifact_url`` for ``build_report_context``: resolves inline-image nodes to a placeholder
    that bakes in the artifact's storage_path (see ``render_html.make_inline_artifact_url``)."""
    by_id = {a.id: a.storage_path for a in engagement.artifacts}

    def _url(artifact_id: int) -> str:
        return make_inline_artifact_url(by_id.get(artifact_id))

    return _url


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").lower()
    return slug or "report"


def register(api_bp, bp) -> None:
    """Attach the report routes to the UI blueprint ``bp``.

    ``api_bp`` is accepted to match the WS7 route-registration contract; the report is served as
    HTML/binary (not JSON), so no routes are added to the JSON API blueprint today.
    """

    @bp.get("/engagements/<int:engagement_id>/report")
    def engagement_report(engagement_id: int):
        cfg = get_config()
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            authorize_engagement_view(engagement)
            ctx = build_report_context(engagement, artifact_url=_artifact_url_factory(engagement))
            html_doc = render_report_html(
                ctx,
                inline_assets=True,
                artifact_bytes=_make_artifact_bytes(cfg.artifact_root),
                engagement_url=url_for("scribble.engagement_board", engagement_id=engagement_id),
                dashboard_url=url_for("scribble.dashboard"),
            )
        return Response(html_doc, mimetype="text/html")

    @bp.get("/engagements/<int:engagement_id>/report/export")
    def engagement_report_export(engagement_id: int):
        cfg = get_config()
        fmt = (request.args.get("format") or "html").strip().lower()
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            authorize_engagement_view(engagement)
            ctx = build_report_context(engagement, artifact_url=_artifact_url_factory(engagement))
            artifact_bytes = _make_artifact_bytes(cfg.artifact_root)
            slug = _slugify(engagement.name)

            if fmt == "zip":
                payload = export_zip(ctx, artifact_bytes)
                return Response(
                    payload,
                    mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{slug}-report.zip"'},
                )

            html_doc = render_report_html(
                ctx,
                inline_assets=True,
                artifact_bytes=artifact_bytes,
                engagement_url=url_for("scribble.engagement_board", engagement_id=engagement_id),
                dashboard_url=url_for("scribble.dashboard"),
            )

        return Response(
            html_doc,
            mimetype="text/html",
            headers={"Content-Disposition": f'attachment; filename="{slug}-report.html"'},
        )
