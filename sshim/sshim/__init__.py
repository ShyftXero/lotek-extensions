"""sshim — a disposable SSH-only pivot generator, mountable into lotek or any Flask app.

"Poor man's VPN to a target network over SSH, for a quick task." Two machines on hostile networks
rendezvous through an SSH relay using only the resident ``ssh`` client; when both sides hang up, it's
gone. This is **Phase 1**: the rendezvous is a 3rd-party box the operator configures (a VPS / tailnet
node / ``shyft.us``), so sshim needs **zero lotek-core changes** and imports **nothing** from ``app.*``.

Everything host-specific is injected through :func:`register` (identity / audit / settings / the PAT
trio), never imported, so the package builds and tests standalone and slots into lotek unchanged. Phase 2
(lotek#1747) will later let a profile request a lotek-hosted ephemeral relay through ``extras`` when that
seam lands; ``deps.host_hook`` is the single place that will read it.
"""

from __future__ import annotations

from pathlib import Path

from sshim._version import __version__
from sshim.config import SshimConfig
from sshim.db import create_all, make_session_factory

__all__ = ["register", "SshimConfig", "__version__"]


def register(
    app,
    engine,
    *,
    url_prefix: str = "/sshim",
    instance_path: str | Path | None = None,
    base_template: str = "sshim/base.html",
    session_factory=None,
    create_tables: bool = True,
    **_host_models,
) -> SshimConfig:
    """Attach sshim to ``app`` against ``engine`` and return its :class:`SshimConfig`.

    In lotek: the mount framework passes the host engine + session factory + ``base_template='base.html'``.
    ``**_host_models`` absorbs the host models sshim does not use — a pivot is operator infrastructure,
    not engagement data.
    """
    # Import blueprints here (once per process, before register_blueprint) to satisfy Flask's
    # "no routes after first request" rule for every app this process builds.
    from sshim.api_pat import machine_bp
    from sshim.blueprint import bp

    inst = Path(instance_path) if instance_path else Path(app.instance_path)
    inst.mkdir(parents=True, exist_ok=True)

    sf = session_factory or make_session_factory(engine)
    if create_tables:
        create_all(engine)

    cfg = SshimConfig(
        session_factory=sf,
        engine=engine,
        instance_path=inst,
        url_prefix=url_prefix,
        base_template=base_template,
    )
    app.extensions["sshim"] = cfg

    # ORDER MATTERS (same rule as bugreport): the host injects its capabilities into `cfg.extras` only
    # AFTER this returns, so register the fail-CLOSED machine surface first and the browser page last —
    # a partial mount can then only leave a 503, never an open page.
    app.register_blueprint(machine_bp, url_prefix=f"{url_prefix}/machine")
    app.register_blueprint(bp, url_prefix=url_prefix)
    return cfg
