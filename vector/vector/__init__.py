"""Vector — a mountable attack-path visualization package.

Mount into any Flask app (standalone host or lotek) with :func:`register`. Everything host-specific is
injected here, never imported, so the package builds/tests standalone and slots into lotek unchanged.
"""

from __future__ import annotations

from pathlib import Path

from vector._version import __version__
from vector.config import VectorConfig
from vector.db import create_all, make_session_factory

__all__ = ["register", "VectorConfig", "__version__"]


def register(
    app,
    engine,
    *,
    url_prefix: str = "/vector",
    instance_path: str | Path | None = None,
    base_template: str = "vector/base.html",
    client_model=None,
    session_factory=None,
    create_tables: bool = True,
    **_host_models,
) -> VectorConfig:
    """Attach Vector to ``app`` against ``engine`` and return its :class:`VectorConfig`.

    In lotek: pass the host engine + session factory + ``client_model`` + ``base_template='base.html'``.
    Standalone: ``vector.standalone`` passes Vector's own engine and base template.

    ``**_host_models`` absorbs any extra host models the mount framework passes positionally-by-keyword
    (e.g. ``asset_model``/``severity_enum``) that Vector doesn't use — accepting them keeps Vector
    compatible with a host that injects the full bundle without Vector having to know about each one.
    """
    # Blueprints are module singletons whose routes are wired by decorators at import time, so importing
    # them here (once per process, before register_blueprint) satisfies Flask's "no routes after
    # registration" rule for every app this process builds (tests, multi-app hosts).
    from vector.api import api_bp
    from vector.api_pat import machine_bp
    from vector.blueprint import bp

    inst = Path(instance_path) if instance_path else Path(app.instance_path)
    inst.mkdir(parents=True, exist_ok=True)

    sf = session_factory or make_session_factory(engine)
    if create_tables:
        create_all(engine)

    cfg = VectorConfig(
        session_factory=sf,
        engine=engine,
        instance_path=inst,
        url_prefix=url_prefix,
        base_template=base_template,
        client_model=client_model,
    )
    app.extensions["vector"] = cfg

    app.register_blueprint(bp, url_prefix=url_prefix)
    app.register_blueprint(api_bp, url_prefix=f"{url_prefix}/api")
    # PAT/Bearer machine API — a SEPARATE blueprint at a prefix DISJOINT from the cookie-authed /api
    # above: the host exempts this prefix from CSRF + its session gate (manifest [host] machine_prefix),
    # which must never apply to the browser surface.
    app.register_blueprint(machine_bp, url_prefix=f"{url_prefix}/machine")
    return cfg
