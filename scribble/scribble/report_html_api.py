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

from flask import Response, abort, request, url_for
from sqlalchemy import select

from scribble.artifacts_storage import artifact_bytes
from scribble.authz import authorize_engagement_view
from scribble.deps import open_session
from scribble.models import Engagement, ScribbleSettings, ScribbleThemeOverride
from scribble.reporting.context import build_report_context
from scribble.reporting.render_html import export_zip, make_inline_artifact_url, render_report_html

# CRIT-4's tenancy predicate now lives in ``scribble/authz.py`` (it's also the primitive the
# blueprint-wide ``before_request`` gate uses — see that module's docstring for the full history).
# ``report_docx_api.py`` imports it from there directly, not from this module.


def _artifact_url_factory(engagement: Engagement) -> Callable[[int], str]:
    """``artifact_url`` for ``build_report_context``: resolves inline-image nodes to a placeholder
    that bakes in the artifact's storage_path (see ``render_html.make_inline_artifact_url``)."""
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



def _override_theme_sources(db):
    """``(lookup, names, install_default)`` for this install's Theme configuration.

    ``reporting/`` never touches a session -- the renderers are pure functions over a frozen
    ``ReportContext``, which is what lets the whole report suite run with no database. So an override
    Theme reaches the renderer the same way evidence bytes do: as an injected callable plus the list of
    names the switcher should offer. A caller with no database (a test, a standalone render) passes
    neither and gets bundled + installed Themes only.

    Names are CASE-FOLDED on the way out. `theme_registry.resolve_theme` lower-cases the requested
    name and the switcher renders the folded name as its `<option value>`, so a row stored as `Acme`
    would be offered as `acme` and then fail to look up -- silently falling back to `auto`, which is a
    Theme that appears in the list and does nothing when picked. Folding here fixes existing rows;
    `themes_api` also folds at write time so new ones are canonical.
    """
    # NAMES ONLY for the switcher, and the payload fetched LAZILY for the one Theme actually selected.
    # Loading every override Theme's full `source_toml` on every report render and every export -- to
    # use at most one of them -- grew the per-render cost with the number of Themes an install had
    # accumulated, for no benefit. The switcher needs names; the renderer needs one payload.
    names = tuple(
        sorted(
            (n or "").strip().lower()
            for n in db.scalars(select(ScribbleThemeOverride.name))
        )
    )

    def lookup(name: str) -> str | None:
        """Fetch one override Theme's TOML. Closes over the request's session, which is still open:
        the renderer is called inside the same `with open_session()` block."""
        folded = (name or "").strip().lower()
        if folded not in names:
            return None  # spares a query for the overwhelmingly common bundled/installed case
        return db.scalar(
            select(ScribbleThemeOverride.source_toml).where(ScribbleThemeOverride.name == folded)
        )

    settings = db.scalar(select(ScribbleSettings).where(ScribbleSettings.slot == "default"))
    install_default = (settings.default_report_theme if settings else None) or None
    return lookup, names, install_default


def _selected_theme(install_default: str | None) -> str | None:
    """The Theme name to render with: an explicit `?theme=` wins, else this install's default.

    Without this the per-install default was settable, validated and audited but had NO READER -- an
    admin could pick the Theme every report inherits and nothing inherited it. An explicit query value
    still wins, so the switcher keeps working and a shared report URL keeps meaning what it says.
    """
    requested = (request.args.get("theme") or "").strip()
    return requested or install_default


def register(api_bp, bp) -> None:
    """Attach the report routes to the UI blueprint ``bp``.

    ``api_bp`` is accepted to match the WS7 route-registration contract; the report is served as
    HTML/binary (not JSON), so no routes are added to the JSON API blueprint today.
    """

    @bp.get("/engagements/<uuid:engagement_id>/report")
    def engagement_report(engagement_id: int):
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            authorize_engagement_view(engagement)
            _override_lookup, _override_names, _install_default = _override_theme_sources(db)
            ctx = build_report_context(engagement, artifact_url=_artifact_url_factory(engagement))
            html_doc = render_report_html(
                ctx,
                inline_assets=True,
                artifact_bytes=artifact_bytes,
                engagement_url=url_for("scribble.engagement_board", engagement_id=engagement_id),
                dashboard_url=url_for("scribble.dashboard"),
                layout=request.args.get("layout"),
                theme=_selected_theme(_install_default),
                template=request.args.get("template"),
                override_lookup=_override_lookup,
                override_theme_names=_override_names,
            )
        return Response(html_doc, mimetype="text/html")

    @bp.get("/engagements/<uuid:engagement_id>/report/export")
    def engagement_report_export(engagement_id: int):
        fmt = (request.args.get("format") or "html").strip().lower()
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                abort(404)
            authorize_engagement_view(engagement)
            _override_lookup, _override_names, _install_default = _override_theme_sources(db)
            ctx = build_report_context(engagement, artifact_url=_artifact_url_factory(engagement))
            slug = _slugify(engagement.name)

            if fmt == "zip":
                payload = export_zip(
                    ctx,
                    artifact_bytes,
                    layout=request.args.get("layout"),
                    theme=_selected_theme(_install_default),
                    template=request.args.get("template"),
                    override_lookup=_override_lookup,
                )
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
                layout=request.args.get("layout"),
                theme=_selected_theme(_install_default),
                template=request.args.get("template"),
                override_lookup=_override_lookup,
                override_theme_names=_override_names,
            )

        return Response(
            html_doc,
            mimetype="text/html",
            headers={"Content-Disposition": f'attachment; filename="{slug}-report.html"'},
        )
