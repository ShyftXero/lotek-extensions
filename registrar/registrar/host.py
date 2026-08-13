"""Access to the mounting HOST's injected PAT capabilities — resolved LATE, per request.

Mirror of scribble/host.py. Reads ``registrar.deps.get_config().extras`` at call time and FAILS CLOSED
(503) when the host injected nothing — standalone Registrar has no PAT scheme, so a machine route with no
host gate must refuse, never run unauthenticated. Only ``registrar/api_pat.py`` uses this.
"""

from __future__ import annotations

import functools
from typing import Any

from flask import jsonify

from registrar.deps import get_config

# Cross-cutting HOST convention (documented in app/api_schemas.py): the host's OpenAPI generator treats a
# route as a PAT-drivable machine endpoint iff its registered view carries this attribute, and reads the
# required scope from it. `require_scope` stamps it. Literal because an extension must not import a host.
SCOPE_ATTR = "__lotek_scope__"


def host_hook(name: str) -> Any | None:
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
    ``.scopes``), or None. ``.role`` is the role *value* string ("viewer"|"operator"|"admin")."""
    hook = host_hook("pat_actor")
    return hook() if hook is not None else None
