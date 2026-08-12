"""Scribble — a mountable pentest vuln-DB + reporting package.

Mount into any Flask app (standalone host or Lotek) with :func:`register`. Everything host-specific is
injected here, never imported, so the package builds/tests standalone and slots into Lotek unchanged.
"""

from __future__ import annotations

from pathlib import Path

from scribble._version import __version__
from scribble.config import ScribbleConfig
from scribble.db import create_all, make_session_factory

__all__ = ["register", "ScribbleConfig", "__version__"]

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
    from scribble.artifacts_api import register as _artifacts
    from scribble.assessment_types_ui import register as _assessment_types_ui
    from scribble.autosave_api import register as _autosave
    from scribble.checklists_api import register as _checklists
    from scribble.collab.crdt import register as _collab
    from scribble.collab.presence import register as _presence
    from scribble.engagement_ui import register as _engagement_ui
    from scribble.library_ui import register as _library_ui
    from scribble.report_docx_api import register as _report_docx
    from scribble.report_html_api import register as _report_html
    from scribble.templating_api import register as _templating

    for hook in (
        _library_ui,
        _engagement_ui,
        _assessment_types_ui,
        _artifacts,
        _checklists,
        _templating,
        _report_html,
        _report_docx,
        _autosave,
        _presence,
        _collab,
    ):
        hook(api_bp, bp)

    # Blueprint-wide fail-closed tenancy gate (scribble/authz.py) — covers every OTHER engagement-scoped
    # route the feature hooks above just added (only the report routes called the tenancy check
    # directly before this existed). Registered here, after every route is on the blueprint but before
    # the first ``app.register_blueprint`` call, same timing constraint as the routes themselves.
    from scribble.authz import register_gate

    register_gate(api_bp, bp)

    # PAT/Bearer machine API (scribble/api_pat.py) — its OWN blueprint, its own hook signature (no
    # ``api_bp``/``bp`` argument: it never touches the cookie-authed surfaces those two carry).
    from scribble.api_pat import register as _api_pat

    _api_pat(machine_bp)
    _FEATURE_ROUTES_WIRED = True


def register(
    app,
    engine,
    *,
    url_prefix: str = "/scribble",
    instance_path: str | Path | None = None,
    base_template: str = "scribble/base.html",
    client_model=None,
    asset_model=None,
    severity_enum=None,
    session_factory=None,
    create_tables: bool = True,
) -> ScribbleConfig:
    """Attach Scribble to ``app`` against ``engine``.

    In Lotek: pass the host engine + session factory + ``client_model`` + ``base_template='base.html'``.
    Standalone: ``standalone_app`` passes Scribble's own engine and base template.
    """
    from scribble.api import api_bp
    from scribble.api_pat import machine_bp
    from scribble.blueprint import bp

    inst = Path(instance_path) if instance_path else Path(app.instance_path)
    (inst / "artifacts").mkdir(parents=True, exist_ok=True)

    sf = session_factory or make_session_factory(engine)
    if create_tables:
        create_all(engine)

    cfg = ScribbleConfig(
        session_factory=sf,
        engine=engine,
        instance_path=inst,
        url_prefix=url_prefix,
        base_template=base_template,
        client_model=client_model,
        asset_model=asset_model,
        severity_enum=severity_enum,
    )
    app.extensions["scribble"] = cfg

    _wire_feature_routes(api_bp, bp, machine_bp)
    app.register_blueprint(bp, url_prefix=url_prefix)
    app.register_blueprint(api_bp, url_prefix=f"{url_prefix}/api")
    # PAT/Bearer machine API. A SEPARATE blueprint at a prefix DISJOINT from the cookie-authed ``/api``
    # one above: the host exempts this prefix from CSRF + its session gate (manifest
    # ``[host] machine_prefix``), which must never apply to the browser surface.
    app.register_blueprint(machine_bp, url_prefix=f"{url_prefix}/machine")
    return cfg
