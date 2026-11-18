"""PASTEL — paste store with expiry, read-count limits, and password protection.

Everything host-specific is injected through :func:`register`; the package builds and tests
standalone and slots into lotek unchanged.
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

    In lotek: the mount framework passes the host engine + session factory + base_template='base.html'.
    ``**_host_models`` absorbs host models this extension does not use.
    """
    from pastel.api_machine import machine_bp
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

    # Register machine API FIRST (fails closed on empty extras), browser blueprint second.
    app.register_blueprint(machine_bp, url_prefix=f"{url_prefix}/machine")
    app.register_blueprint(bp, url_prefix=url_prefix)
    return cfg
