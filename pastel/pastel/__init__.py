"""PASTEL — markdown pastes behind unguessable GUID URLs, mountable into lotek or any Flask app.

A user (or a runner, via the PAT machine API) creates a paste; readers open it by its unguessable GUID.
Optional access controls — expiry, a read-count limit, and a password — hang off each paste and compose.
Everything host-specific is injected through :func:`register`, never imported, so the package builds and
tests standalone and slots into lotek unchanged.
"""

from __future__ import annotations

from pathlib import Path

from pastel._version import __version__
from pastel.config import PastelConfig
from pastel.db import create_all, make_session_factory

__all__ = ["register", "PastelConfig", "__version__"]


def register(
    app,
    engine,
    *,
    url_prefix: str = "/pastel",
    instance_path: str | Path | None = None,
    base_template: str = "pastel/base.html",
    session_factory=None,
    create_tables: bool = True,
    **_host_models,
) -> PastelConfig:
    """Attach Pastel to ``app`` against ``engine`` and return its :class:`PastelConfig`.

    In lotek: the mount framework passes the host engine + session factory + ``base_template='base.html'``.
    ``**_host_models`` absorbs the host models (client/asset/severity) Pastel does not use.
    """
    # Import blueprints here (once per process, before register_blueprint) to satisfy Flask's "no routes
    # after first request" rule for every app this process builds.
    from pastel.api_pat import machine_bp
    from pastel.blueprint import bp

    inst = Path(instance_path) if instance_path else Path(app.instance_path)
    inst.mkdir(parents=True, exist_ok=True)

    sf = session_factory or make_session_factory(engine)
    if create_tables:
        create_all(engine)

    cfg = PastelConfig(
        session_factory=sf,
        engine=engine,
        instance_path=inst,
        url_prefix=url_prefix,
        base_template=base_template,
    )
    app.extensions["pastel"] = cfg

    # ORDER MATTERS. The host injects its capabilities into `cfg.extras` only AFTER this returns, so
    # between here and there `extras` is empty — and the two blueprints degrade in OPPOSITE directions on
    # an empty `extras`: the machine API 503s (closed), while the browser page reads "no current_actor
    # hook" as standalone and treats every dashboard user as admin (open). Register the fail-CLOSED
    # surface first so a partial mount can only ever leave a 503, never an open page.
    app.register_blueprint(machine_bp, url_prefix=f"{url_prefix}/machine")
    app.register_blueprint(bp, url_prefix=url_prefix)
    return cfg
