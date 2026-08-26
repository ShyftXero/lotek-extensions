"""BUGREPORT — text-only bug report capture, mountable into lotek or any Flask app.

Users and agents file "here is an aspect of a bug"; it is CAPTURED, not forwarded anywhere. A user CRUDs
their own reports, an admin responds to (and tombstones) anyone's, and the reporter sees what the admin
did. Everything host-specific is injected through :func:`register`, never imported, so the package builds
and tests standalone and slots into lotek unchanged.
"""

from __future__ import annotations

from pathlib import Path

from bugreport._version import __version__
from bugreport.config import BugreportConfig
from bugreport.db import create_all, make_session_factory

__all__ = ["register", "BugreportConfig", "__version__"]


def register(
    app,
    engine,
    *,
    url_prefix: str = "/bugreport",
    instance_path: str | Path | None = None,
    base_template: str = "bugreport/base.html",
    session_factory=None,
    create_tables: bool = True,
    **_host_models,
) -> BugreportConfig:
    """Attach Bugreport to ``app`` against ``engine`` and return its :class:`BugreportConfig`.

    In lotek: the mount framework passes the host engine + session factory + ``base_template='base.html'``.
    ``**_host_models`` absorbs the host models (client/asset/severity) that Bugreport does not use — a
    bug report is platform feedback, not engagement data.
    """
    # Import blueprints here (once per process, before register_blueprint) to satisfy Flask's
    # "no routes after first request" rule for every app this process builds.
    from bugreport.api_pat import machine_bp
    from bugreport.blueprint import bp

    inst = Path(instance_path) if instance_path else Path(app.instance_path)
    inst.mkdir(parents=True, exist_ok=True)

    sf = session_factory or make_session_factory(engine)
    if create_tables:
        create_all(engine)

    cfg = BugreportConfig(
        session_factory=sf,
        engine=engine,
        instance_path=inst,
        url_prefix=url_prefix,
        base_template=base_template,
    )
    app.extensions["bugreport"] = cfg

    app.register_blueprint(bp, url_prefix=url_prefix)
    # PAT/Bearer machine API — SEPARATE blueprint, prefix DISJOINT from the browser page. The host exempts
    # this prefix from CSRF + its session gate (manifest [host] machine_prefix), so the blueprint's own
    # before_request/scope gates are the ONLY thing standing in front of it.
    app.register_blueprint(machine_bp, url_prefix=f"{url_prefix}/machine")
    return cfg
