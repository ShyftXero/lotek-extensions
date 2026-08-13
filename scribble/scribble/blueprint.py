"""Human-facing UI blueprint — the shared shell + dashboard.

The library UI (WS2, `scribble/library_ui.py`) and the engagement/board UI (WS3,
`scribble/engagement_ui.py`) contribute their own routes via `register(api_bp, bp)` hooks wired in
`scribble/__init__.py`, so those workstreams own disjoint files. Route names are a FROZEN CONTRACT
(see plans/CONTRACTS.md §7): `scribble.dashboard`, `scribble.library`, `scribble.engagements`,
`scribble.engagement_board`.
"""

from __future__ import annotations

from flask import Blueprint, render_template
from sqlalchemy import func, select

from scribble._version import __version__
from scribble.authz import filter_visible_engagements, host_is_mounted
from scribble.deps import (
    client_names,
    current_actor,
    get_config,
    host_can_write,
    open_session,
)
from scribble.models import Engagement, EngagementFinding, VulnerabilityTemplate

bp = Blueprint("scribble", __name__, template_folder="templates", static_folder="static")


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "scribble_base": cfg.base_template,
        "scribble_version": __version__,
        # Viewer read-only nudge (docs/LOTEK_ADOPTION.md §4.1): the host's own request-method/role gate
        # is the real enforcement; this only lets templates hide/disable controls that would 400/403 if
        # clicked. True (writable) when no host hook is present -- see scribble.deps.host_can_write.
        "scribble_can_write": host_can_write(),
        # Whether a host bundle is mounted. Templates use it to drop controls that are only coherent
        # standalone -- today, the create/edit forms' "or a new client name" field: mounted, clients live
        # in the HOST's table and are the host's to create (see engagement_ui._resolve_client, which
        # refuses it server-side; this only keeps the form from offering what the server will decline).
        "scribble_host_mounted": host_is_mounted(),
    }


@bp.get("/")
def dashboard():
    """The landing page — scoped to the viewer's own clients.

    It used to list the 10 most recent engagements across EVERY client, over global `SELECT count(*)`
    stat tiles (engagements/findings/clients). That is the same cross-tenant read the by-id gate closes,
    in its cheapest form: no id to guess, the names and client names of other tenants' engagements simply
    rendered. `scribble.authz.filter_visible_engagements` scopes the list; the counts are then DERIVED
    from that same visible set rather than re-queried globally, so the tiles can't drift back out of
    scope independently of the list under them.

    The one deliberately global tile is Vuln Templates: the library is a shared, tenant-free table (the
    same reason its routes carry no engagement axis at all — see `library_ui.py`).
    """
    with open_session() as db:
        rows = db.scalars(select(Engagement).order_by(Engagement.created_at.desc())).all()
        visible = filter_visible_engagements(rows, current_actor())
        visible_ids = [e.id for e in visible]
        findings = 0
        if visible_ids:
            findings = (
                db.scalar(
                    select(func.count())
                    .select_from(EngagementFinding)
                    .where(EngagementFinding.engagement_id.in_(visible_ids))
                )
                or 0
            )
        counts = {
            "engagements": len(visible),
            "templates": db.scalar(select(func.count()).select_from(VulnerabilityTemplate)) or 0,
            # Clients the viewer actually has engagements under -- NOT a count of the mounted client
            # table (docs/LOTEK_ADOPTION.md §3.1), which would report how many clients exist to someone
            # holding a grant under one of them.
            "clients": len({e.client_id for e in visible if e.client_id is not None}),
            "findings": findings,
        }
        engagements = visible[:10]
        return render_template(
            "scribble/dashboard.html",
            counts=counts,
            engagements=engagements,
            client_names=client_names(db, engagements),
        )
