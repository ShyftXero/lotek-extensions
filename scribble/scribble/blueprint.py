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
from scribble.deps import client_model, client_names, get_config, host_can_write, open_session
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
    }


@bp.get("/")
def dashboard():
    with open_session() as db:
        counts = {
            "engagements": db.scalar(select(func.count()).select_from(Engagement)) or 0,
            "templates": db.scalar(select(func.count()).select_from(VulnerabilityTemplate)) or 0,
            # client_model(): counts the mounted client table (the host's, when injected -- see
            # docs/LOTEK_ADOPTION.md §3.1), not always scribble_clients, which stays empty when mounted.
            "clients": db.scalar(select(func.count()).select_from(client_model())) or 0,
            "findings": db.scalar(select(func.count()).select_from(EngagementFinding)) or 0,
        }
        engagements = db.scalars(select(Engagement).order_by(Engagement.created_at.desc()).limit(10)).all()
        return render_template(
            "scribble/dashboard.html",
            counts=counts,
            engagements=engagements,
            client_names=client_names(db, engagements),
        )
