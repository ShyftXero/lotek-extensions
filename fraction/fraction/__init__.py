"""Fraction — a mountable pentest vuln-DB + reporting package.

Mount into any Flask app (standalone host or Lotek) with :func:`register`. Everything host-specific is
injected here, never imported, so the package builds/tests standalone and slots into Lotek unchanged.
"""

from __future__ import annotations

from pathlib import Path

from fraction._version import __version__
from fraction.config import FractionConfig
from fraction.db import create_all, make_session_factory

__all__ = ["register", "FractionConfig", "__version__"]

# Feature routes (artifacts / templating / report / autosave / presence) are contributed by each
# workstream via a ``register(api_bp, bp)`` hook. They are added to the module-singleton blueprints
# ONCE per process — before the first ``app.register_blueprint`` — because Flask forbids adding routes
# to a blueprint after it has been registered on any app. The guard makes multi-app setups (tests,
# a host that builds several apps) safe.
_FEATURE_ROUTES_WIRED = False


def _wire_feature_routes(api_bp, bp, machine_bp) -> None:
    global _FEATURE_ROUTES_WIRED
    if _FEATURE_ROUTES_WIRED:
        return
    from fraction.artifacts_api import register as _artifacts
    from fraction.assessment_types_ui import register as _assessment_types_ui
    from fraction.autosave_api import register as _autosave
    from fraction.collab.crdt import register as _collab
    from fraction.collab.presence import register as _presence
    from fraction.engagement_ui import register as _engagement_ui
    from fraction.library_ui import register as _library_ui
    from fraction.report_docx_api import register as _report_docx
    from fraction.report_html_api import register as _report_html
    from fraction.templating_api import register as _templating

    for hook in (
        _library_ui,
        _engagement_ui,
        _assessment_types_ui,
        _artifacts,
        _templating,
        _report_html,
        _report_docx,
        _autosave,
        _presence,
        _collab,
    ):
        hook(api_bp, bp)

    # PAT/Bearer machine API (fraction/api_pat.py) — its OWN blueprint, its own hook signature (no
    # ``api_bp``/``bp`` argument: it never touches the cookie-authed surfaces those two carry).
    from fraction.api_pat import register as _api_pat

    _api_pat(machine_bp)
    _FEATURE_ROUTES_WIRED = True


def register(
    app,
    engine,
    *,
    url_prefix: str = "/fraction",
    instance_path: str | Path | None = None,
    base_template: str = "fraction/base.html",
    client_model=None,
    asset_model=None,
    severity_enum=None,
    session_factory=None,
    create_tables: bool = True,
) -> FractionConfig:
    """Attach Fraction to ``app`` against ``engine``.

    In Lotek: pass the host engine + session factory + ``client_model`` + ``base_template='base.html'``.
    Standalone: ``standalone_app`` passes Fraction's own engine and base template.
    """
    from fraction.api import api_bp
    from fraction.api_pat import machine_bp
    from fraction.blueprint import bp

    inst = Path(instance_path) if instance_path else Path(app.instance_path)
    (inst / "artifacts").mkdir(parents=True, exist_ok=True)

    sf = session_factory or make_session_factory(engine)
    if create_tables:
        create_all(engine)

    cfg = FractionConfig(
        session_factory=sf,
        engine=engine,
        instance_path=inst,
        url_prefix=url_prefix,
        base_template=base_template,
        client_model=client_model,
        asset_model=asset_model,
        severity_enum=severity_enum,
    )
    app.extensions["fraction"] = cfg

    _wire_feature_routes(api_bp, bp, machine_bp)
    app.register_blueprint(bp, url_prefix=url_prefix)
    app.register_blueprint(api_bp, url_prefix=f"{url_prefix}/api")
    # PAT/Bearer machine API. A SEPARATE blueprint at a prefix DISJOINT from the cookie-authed ``/api``
    # one above: the host exempts this prefix from CSRF + its session gate (manifest
    # ``[host] machine_prefix``), which must never apply to the browser surface.
    app.register_blueprint(machine_bp, url_prefix=f"{url_prefix}/machine")
    return cfg
