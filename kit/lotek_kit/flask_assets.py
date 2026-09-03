"""Serve the kit's browser assets from whatever Flask app asks for them.

A wheel's package data sits on no URL. This registers a blueprint that puts it on one, and it is the
only module in the kit that touches Flask — which is why ``flask`` is an optional extra and the import
happens **inside** the function. ``import lotek_kit.attackpath`` must work in an environment with no web
framework installed, and a test pins that.

**The endpoint name is fixed; the prefix is not.** Templates always write
``url_for("lotek_kit.static", filename="reorder.js")``, which resolves identically whether the kit is
mounted under ``/_kit`` in core or under ``/scribble/kit`` inside an extension. That is the whole point
of registering a blueprint rather than handing consumers a URL to interpolate: the caller chooses where
the assets live, and nothing downstream has to know.

**Registration is idempotent, and that is mandatory rather than defensive.** Flask raises
``ValueError: The name 'lotek_kit' is already registered`` on a second registration of the same
blueprint name, and Flask has no unregister. With core and one or more extensions all calling this
during startup, second-caller-wins would take the app down at boot.
"""

from __future__ import annotations

from typing import Any

#: One name across every host, because ``url_for("lotek_kit.static", ...)`` is a template-level
#: contract. Changing it breaks every consumer's markup at once.
BLUEPRINT_NAME = "lotek_kit"

DEFAULT_URL_PREFIX = "/_kit"


def registered_prefix(app: Any) -> str | None:
    """The URL prefix the kit's assets are actually served under, or None if unregistered.

    Read out of the URL map rather than off ``app.blueprints[BLUEPRINT_NAME].url_prefix``: that
    attribute reflects how the Blueprint object was *constructed*, not how it was registered, and is
    ``None`` for a blueprint whose prefix was supplied at registration time — which is every caller here.
    """
    for rule in app.url_map.iter_rules():
        if rule.endpoint == f"{BLUEPRINT_NAME}.static":
            # The rule is "<prefix>/<path:filename>"; everything before the converter is the prefix.
            return rule.rule.split("<", 1)[0].rstrip("/")
    return None


def ensure_registered(app: Any, url_prefix: str = DEFAULT_URL_PREFIX) -> str:
    """Register the kit's asset blueprint on ``app`` if it is not already there.

    Returns the prefix actually in effect — **first caller wins**, so a second caller passing a
    different prefix gets the first one back rather than an exception or a silently duplicated mount.
    Check the return value if you intend to build URLs by hand; use ``url_for`` and you do not have to.

    Call this BEFORE anything else in an extension's ``register()``. Not for tidiness: if a later
    statement raises, ``mount_extensions`` catches it and carries on with the extension's own blueprint
    already irreversibly registered and its authorization extras never injected. Anything that can fail
    belongs after the things that must not be half-done.
    """
    existing = registered_prefix(app)
    if existing is not None:
        return existing

    from flask import Blueprint  # lazy: the kit has no hard Flask dependency

    blueprint = Blueprint(
        BLUEPRINT_NAME,
        __name__,
        static_folder="static",
        # Empty, so the served path is exactly `<url_prefix>/<filename>` with no `/static` segment
        # wedged in the middle.
        static_url_path="",
    )
    app.register_blueprint(blueprint, url_prefix=url_prefix)
    return url_prefix
