"""Unified autosave seam: ONE endpoint, a doc-handle registry, mandatory per-kind authorization.

Every rich-text surface (scribble findings, scribble library templates, vector diagrams, future
engagement prose) used to ship its own autosave route — four writers of "a doc into a column + cache the
rendered HTML", each free to drift. This collapses them: the editor addresses an opaque ``{kind, id,
block}`` handle, one endpoint dispatches through this registry to the kind's ``load``/``save``, gated by
that kind's ``authorize``. Adding autosave to a new surface is "register a kind", not "write a route".

A generic "write field Y of entity X" endpoint CONCENTRATES tenancy risk — one dispatch bug is a hole
across every surface. So the safety is structural, not conventional:

  * ``authorize`` is MANDATORY at register time (omitting it raises here, not in prod),
  * the endpoint fails CLOSED on an unknown kind (404) and an unauthorized actor (403),
  * ``authorize`` must delegate to the SAME tenancy predicate the surface already uses (one home), never a
    second copy — see ``plans/feat-kit-autosave-seam.md`` and the INV-TENANCY invariant.

No Flask at module scope (the kit has no runtime Flask dependency); the blueprint is built lazily in
:func:`ensure_autosave_registered`, mirroring :mod:`lotek_kit.flask_assets`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: Blueprint name — one per app, like the asset blueprint (Flask forbids re-registering a name).
BLUEPRINT_NAME = "lotek_kit_autosave"
#: Default mount. Host-level (the actor is the host's, not any one extension's) and alongside the kit
#: assets at ``/_kit``.
DEFAULT_URL_PREFIX = "/_kit/autosave"


@dataclass(frozen=True)
class AutosaveKind:
    """A registered autosave target type. The kind OWNS its storage + HTML cache; the seam owns the route,
    the dispatch, and the authz gate."""

    load: Callable[[str, str], tuple[Any, str]]   # (id, block) -> (doc, cached_html)
    save: Callable[[str, str, Any], str]          # (id, block, doc) -> freshly rendered html
    authorize: Callable[[Any, str], bool]         # (actor, id) -> bool   (MANDATORY, fail-closed)


#: Process-global registry. Populated once per app by each surface's ``register()``; read per request.
_KINDS: dict[str, AutosaveKind] = {}


def register_autosave_kind(
    kind: str,
    *,
    load: Callable[[str, str], tuple[Any, str]],
    save: Callable[[str, str, Any], str],
    authorize: Callable[[Any, str], bool],
) -> None:
    """Register an autosave target type.

    ``authorize(actor, id) -> bool`` is REQUIRED — a generic write endpoint without a per-kind tenancy
    check is the hole this whole design exists to prevent, so omitting it (or any of load/save) raises HERE
    rather than silently shipping an unauthorized path.
    """
    if not kind or not isinstance(kind, str):
        raise ValueError("autosave kind must be a non-empty str")
    for name, fn in (("load", load), ("save", save), ("authorize", authorize)):
        if not callable(fn):
            raise ValueError(f"autosave kind {kind!r} requires a callable {name} (authz is mandatory)")
    _KINDS[kind] = AutosaveKind(load=load, save=save, authorize=authorize)


def registered_kinds() -> frozenset[str]:
    """The kinds registered so far — for diagnostics and the tenancy invariant's sweep."""
    return frozenset(_KINDS)


def ensure_autosave_registered(
    app: Any,
    *,
    actor: Callable[[], Any],
    url_prefix: str = DEFAULT_URL_PREFIX,
    is_doc: Callable[[Any], bool] | None = None,
) -> str:
    """Mount the single autosave endpoint pair on ``app`` (idempotent; first caller wins, like
    :func:`lotek_kit.flask_assets.ensure_registered`).

    ``actor`` is a zero-arg resolver for the HOST's current principal — a cookie session user OR a PAT
    actor; the seam is auth-mechanism-agnostic and hands whatever it gets to each kind's ``authorize``.
    ``is_doc`` optionally validates a save body (an extension passes its ProseMirror ``schema.is_doc``);
    omitted, any JSON value is accepted and the kind's ``save`` is trusted to cope.
    """
    if BLUEPRINT_NAME in app.blueprints:
        # Already mounted: first caller wins. Return the prefix actually in effect (read off the url map,
        # like flask_assets.registered_prefix), not whatever this later caller happened to pass.
        for rule in app.url_map.iter_rules():
            if rule.endpoint == f"{BLUEPRINT_NAME}.autosave_save":
                return rule.rule
        return url_prefix

    from flask import Blueprint, jsonify, request  # lazy: the kit carries no runtime Flask dependency

    bp = Blueprint(BLUEPRINT_NAME, __name__)

    def _resolve(kind: Any, cid: Any):
        """(AutosaveKind, None) when the actor may touch this target, else (None, (response, status))."""
        k = _KINDS.get(kind) if isinstance(kind, str) else None
        if k is None:
            return None, (jsonify(ok=False, error=f"unknown autosave kind {kind!r}"), 404)
        if not k.authorize(actor(), cid):
            return None, (jsonify(ok=False, error="not authorized"), 403)
        return k, None

    @bp.post(url_prefix)
    def autosave_save():
        body = request.get_json(silent=True) or {}
        kind, cid, block, doc = body.get("kind"), body.get("id"), body.get("block"), body.get("doc")
        if not (kind and cid and block):
            return jsonify(ok=False, error="kind, id and block are required"), 400
        if is_doc is not None and not is_doc(doc):
            return jsonify(ok=False, error="body.doc must be a ProseMirror doc"), 400
        k, err = _resolve(kind, cid)
        if err is not None:
            return err
        return jsonify(ok=True, html=k.save(cid, block, doc))

    @bp.get(url_prefix)
    def autosave_load():
        kind, cid, block = request.args.get("kind"), request.args.get("id"), request.args.get("block")
        if not (kind and cid and block):
            return jsonify(ok=False, error="kind, id and block are required"), 400
        k, err = _resolve(kind, cid)
        if err is not None:
            return err
        doc, html = k.load(cid, block)
        return jsonify(ok=True, doc=doc, html=html)

    app.register_blueprint(bp)
    return url_prefix
