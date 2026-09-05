"""Access to the mounting HOST's injected capabilities — resolved LATE, per request.

Two hard constraints force the indirection:
  * ``scribble.register()`` returns its config and the host fills ``cfg.extras`` AFTERWARDS, so a hook
    is not available while routes are being wired.
  * route wiring is a PROCESS singleton (``scribble._wire_feature_routes``) while ``cfg.extras`` is
    PER-APP, so a hook captured at wiring time would be the wrong app's in a multi-app process (tests).

Everything here therefore reads ``scribble.deps.get_config().extras`` at call time and FAILS CLOSED
(503) when the host injected nothing: standalone Scribble has no PAT scheme, so a machine route with no
host gate must refuse, never run unauthenticated.
"""

from __future__ import annotations

import functools
from typing import Any

from flask import jsonify

from scribble.deps import get_config

# Cross-cutting HOST convention (documented in app/api_schemas.py): the host's OpenAPI generator treats a
# route as a PAT-drivable machine endpoint iff its registered view carries this attribute, and reads the
# required scope from it. `require_scope` stamps it. Spelled as a literal because an extension must not
# import a host module.
SCOPE_ATTR = "__lotek_scope__"


def host_hook(name: str) -> Any | None:
    """One injected host capability by name, or None. Never raises (no app ctx / no host -> None)."""
    try:
        cfg = get_config()
    except RuntimeError:
        return None
    return (cfg.extras or {}).get(name)


def _no_host():
    return jsonify({
        "error": "unavailable",
        "detail": "this machine API requires a mounting host that provides PAT authentication",
    }), 503


def authenticate():
    """Blueprint ``before_request`` hook: delegate to the host's PAT authenticator. Fail closed."""
    hook = host_hook("pat_authenticate")
    if hook is None:
        return _no_host()
    return hook()


def require_scope(scope: str):
    """Per-route decorator: delegate to the host's ``require_pat_scope(scope)`` at REQUEST time, and stamp
    the scope on the wrapper for the host's OpenAPI generator."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            gate = host_hook("require_pat_scope")
            if gate is None:
                return _no_host()
            return gate(scope)(fn)(*args, **kwargs)

        setattr(wrapper, SCOPE_ATTR, scope)
        return wrapper

    return decorator


def actor():
    """The host's authenticated PAT principal (``PatActor``-shaped: ``.id``/``.username``/``.role``/
    ``.scopes``), or None."""
    hook = host_hook("pat_actor")
    return hook() if hook is not None else None


def objects():
    """The host's actor-gated object surface (``put``/``open``/``stat``), or None when unmounted or
    when this deployment has no object store.

    None is a first-class answer: evidence then falls back to local disk, which is where it lived
    before the store existed. Callers must NOT treat None as an error — an operator running without
    SeaweedFS is a supported deployment, not a broken one.
    """
    return host_hook("objects")


def create_engagement(client_id, name):
    """Create the CORE engagement an evidence blob is anchored to, and return its id.

    ``objects.engagement_id`` is NOT NULL for every blob (INV-OBJSTORE-01 makes tenancy a database
    fact via composite FKs), so a scribble engagement with no core engagement behind it has nowhere in
    the bucket to put a file. That is why this exists: not bookkeeping, the anchor.

    MANAGER-OR-ADMIN only in the host — establishing engagement tenancy is privileged there, and the
    seam delegates to the very same code core's own API uses rather than restating the rule. Raises
    ``PermissionError`` when refused and ``ValueError`` when the name is taken for that client;
    neither is a lotek type, so nothing here imports one.

    None when unmounted — but note that persisting evidence then fails outright: standalone Scribble
    is a testbed and its shell must supply a mock host (``scribble.testing.wire_mock_host``).
    """
    hook = host_hook("create_engagement")
    return hook(client_id, name) if hook is not None else None


def can_operate_on(engagement_id) -> bool:
    """Does the current principal hold an operator capability on this CORE engagement?

    The check for a caller that supplies its OWN ``core_engagement_id`` instead of having one created:
    creating an engagement is manager-only, but pointing at one you already operate is not, and
    refusing that would lock every plain operator out of filing evidence.
    """
    hook = host_hook("can_operate_on")
    return bool(hook(engagement_id)) if hook is not None else False


def findings():
    """The host's read-only findings namespace (``get_job``/``list_findings``/``get_finding``), or
    None when unmounted. Callers treat None like an empty host (no scan data reachable)."""
    return host_hook("findings")


def mark_job_promoted(job_id, actor_obj, *, extension: str, ref_id: int) -> bool:
    hook = host_hook("mark_job_promoted")
    if hook is None:
        return False
    return bool(hook(job_id, actor_obj, extension=extension, ref_id=ref_id))


def list_jobs(engagement, actor_obj) -> list:
    """Reverse of ``mark_job_promoted``: the host scan jobs promoted INTO this engagement, each carrying
    ``.id`` (the job ref) and ``.promoted_at``. The host applies its own ``user_can_view_job`` to
    ``actor_obj``. Empty list when unmounted -- standalone Scribble has no jobs to reverse-index."""
    hook = host_hook("list_jobs")
    if hook is None:
        return []
    return list(hook(actor_obj, extension="scribble", ref_id=engagement.id))


def remove_job_adoption(job_id, actor_obj) -> bool:
    """Un-adopt LINK-ONLY (core #632): clear a job's promotion columns so it no longer feeds any
    engagement's Source-jobs panel. The reverse of ``mark_job_promoted``, and the LINK only -- the host
    contract touches no findings, so no promoted data is ever lost here (a destructive un-adopt deletes
    findings on the SCRIBBLE side first, then calls this to drop the link). Idempotent: clearing an
    already-unlinked job is a no-op ``True``. ``False`` when unmounted."""
    hook = host_hook("remove_job_adoption")
    if hook is None:
        return False
    return bool(hook(job_id, actor_obj))
