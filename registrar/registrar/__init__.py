"""REGISTRAR — a mountable client quoting + invoicing package.

Mount into any Flask app (standalone or lotek) with :func:`register`. Everything host-specific is
injected here, never imported, so the package builds/tests standalone and slots into lotek unchanged.

No payment processing — REGISTRAR generates SOW/quotes + invoices; collection happens off-platform.
"""

from __future__ import annotations

from pathlib import Path

from registrar._version import __version__
from registrar.config import RegistrarConfig
from registrar.db import create_all, make_session_factory

__all__ = ["register", "RegistrarConfig", "__version__"]


def register(
    app,
    engine,
    *,
    url_prefix: str = "/registrar",
    instance_path: str | Path | None = None,
    base_template: str = "registrar/base.html",
    client_model=None,
    session_factory=None,
    create_tables: bool = True,
    **_host_models,
) -> RegistrarConfig:
    """Attach REGISTRAR to ``app`` against ``engine`` and return its :class:`RegistrarConfig`.

    In lotek: pass the host engine + session factory + ``client_model`` + ``base_template='base.html'``.
    ``**_host_models`` absorbs any extra host models the mount framework passes that REGISTRAR doesn't use.
    """
    # Import blueprints here (once per process, before register_blueprint) to satisfy Flask's
    # "no routes after first request" rule for every app this process builds.
    from registrar.api import api_bp
    from registrar.api_pat import machine_bp
    from registrar.blueprint import bp

    inst = Path(instance_path) if instance_path else Path(app.instance_path)
    inst.mkdir(parents=True, exist_ok=True)

    sf = session_factory or make_session_factory(engine)
    if create_tables:
        create_all(engine)

    cfg = RegistrarConfig(
        session_factory=sf,
        engine=engine,
        instance_path=inst,
        url_prefix=url_prefix,
        base_template=base_template,
        client_model=client_model,
    )
    app.extensions["registrar"] = cfg

    app.register_blueprint(bp, url_prefix=url_prefix)
    app.register_blueprint(api_bp, url_prefix=f"{url_prefix}/api")
    # PAT/Bearer machine API — SEPARATE blueprint, prefix DISJOINT from the cookie-authed /api. The host
    # exempts this prefix from CSRF + its session gate (manifest [host] machine_prefix). Confirm-tier
    # execution (/approve) is deliberately NOT exposed here (INV-EXT-02: a PAT stages, a human approves).
    app.register_blueprint(machine_bp, url_prefix=f"{url_prefix}/machine")
    return cfg
