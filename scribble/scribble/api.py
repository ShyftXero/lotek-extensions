"""JSON API blueprint (mounted at ``<url_prefix>/api``).

Sprint 0 ships ``/health``. WS4 adds autosave, WS5 adds artifact upload/serve + reorder, WS3 adds board
move/reorder, WS7/WS8 add render endpoints.

⚠ This blueprint is COOKIE-AUTHED and CSRF-PROTECTED, not exempt (CONTRACT.md correction C10). An
earlier version of this docstring claimed "the host exempts it at mount time" -- that was false and
dangerous: the host (lotek) deliberately does NOT exempt ``/scribble/*`` from CSRF (see
``src/app/extensions.py``'s "security > convenience" docstring, and
``test_scribble_cookie_write_is_csrf_protected``, which pins it). The routes that ARE exempt from CSRF
(and from the cookie-session login gate) live in a separate, disjoint, PAT-only blueprint mounted under
the ``[host] machine_prefix`` the extension declares (``/scribble/machine/*`` -- see
``scribble/api_pat.py``), never here.
"""

from __future__ import annotations

from flask import Blueprint, jsonify
from sqlalchemy import func, select

from scribble._version import __version__
from scribble.deps import client_model, open_session
from scribble.models import Engagement, EngagementFinding, VulnerabilityTemplate

api_bp = Blueprint("scribble_api", __name__)


@api_bp.get("/health")
def health():
    with open_session() as db:
        return jsonify(
            status="ok",
            version=__version__,
            counts={
                # client_model(): the mounted client table (the host's, when injected -- see
                # docs/LOTEK_ADOPTION.md §3.1), not always scribble_clients (empty when mounted).
                "clients": db.scalar(select(func.count()).select_from(client_model())) or 0,
                "engagements": db.scalar(select(func.count()).select_from(Engagement)) or 0,
                "templates": db.scalar(select(func.count()).select_from(VulnerabilityTemplate)) or 0,
                "findings": db.scalar(select(func.count()).select_from(EngagementFinding)) or 0,
            },
        )
