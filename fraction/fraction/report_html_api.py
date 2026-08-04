"""WS7 UI routes: the live HTML report + its self-contained/zip export.

Registered onto the existing UI blueprint via :func:`register`, kept in its own module (per
plans/CONTRACTS.md ownership rules — WS7 does not edit ``blueprint.py``/``api.py``/``__init__.py``).
Whoever wires up routes (the driver, or ``fraction/__init__.py`` in a later sprint) calls::

    from fraction.report_html_api import register as register_report_html
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

from fraction.deps import current_actor, get_config, open_session
from fraction.models import Engagement
from fraction.reporting.context import build_report_context
from fraction.reporting.render_html import export_zip, make_inline_artifact_url, render_report_html


def _authorize_engagement_view(engagement: Engagement) -> None:
    """Audit CRIT-4: the report + its export embed a client's findings and evidence artifacts, so this
    route must not serve one to a reader the host would not serve it to.

    **This function no longer decides anything. It asks the host.** It used to carry a hand-copy of
    lotek's predicate — *"mirroring lotek app/access.py user_can_view_job: admins see everything; a
    non-admin sees only engagements it OWNS"*. Every clause of that sentence became false when the
    host moved to per-engagement memberships: there is no admin bypass any more (an admin holds no
    implicit view of engagement data and must self-grant, audited), and ownership was never the axis.
    A stale copy of an access rule does not merely drift — this one **inverted**, granting every
    admin full read plus the creator a read on a client it may hold no membership under. That is the
    argument against copying a predicate, and the host now exposes ``can_view_client`` so there is
    nothing left to copy.

    Note the trap that makes the copy so easy to write: Fraction's ``Engagement.owner_id`` is
    ATTRIBUTION only (engagements are team-shared — see the model), whereas the host's
    ``Job.owner_id`` used to be the gate. The host has now inverted its own column to match
    Fraction's meaning. Neither is an authorization key; do not reintroduce either as one.

    Standalone Fraction (no host bundle) has no host authorization model, so nothing is enforced
    there — unchanged. With a host wired, this fails CLOSED (404, never 403: do not confirm that the
    engagement id exists to someone who may not read it).
    """
    cfg = get_config()
    if not cfg.extras.get("host"):
        return  # standalone Fraction — no host authorization model to apply
    can_view_client = cfg.extras.get("can_view_client")
    if can_view_client is None:
        # A host bundle that predates the contract. Refuse rather than fall back to a local rule:
        # the whole point is that this module holds no policy of its own to fall back TO.
        abort(404)
    if not can_view_client(getattr(engagement, "client_id", None), current_actor()):
        abort(404)


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
            _authorize_engagement_view(engagement)
            ctx = build_report_context(engagement, artifact_url=_artifact_url_factory(engagement))
            html_doc = render_report_html(
                ctx,
                inline_assets=True,
                artifact_bytes=_make_artifact_bytes(cfg.artifact_root),
                engagement_url=url_for("fraction.engagement_board", engagement_id=engagement_id),
                dashboard_url=url_for("fraction.dashboard"),
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
            _authorize_engagement_view(engagement)
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
                engagement_url=url_for("fraction.engagement_board", engagement_id=engagement_id),
                dashboard_url=url_for("fraction.dashboard"),
            )

        return Response(
            html_doc,
            mimetype="text/html",
            headers={"Content-Disposition": f'attachment; filename="{slug}-report.html"'},
        )
