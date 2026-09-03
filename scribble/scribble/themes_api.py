"""Operator-supplied **override** report Themes — persistence, validated upload, CRUD, and the
per-install default (ext#113 + #105).

``CONTEXT.md``'s Provenance entry names three kinds of Theme: **bundled** (ships inside
``reporting/theme_files.py``'s ``report_themes/`` package), **installed** (a separate Python package
Scribble discovers — ``reporting/theme_discovery.py``), and **override** — data an operator supplied at
runtime. This module is the admin surface for the last one: it is the only place in Scribble that
WRITES a Theme, as opposed to reading one that arrived as code.

## One validator, reused, never re-implemented

Every write here goes through ``reporting.theme_files._parse_theme_toml`` — the SAME pure parser a
bundled ``.toml`` file is checked against — before anything reaches a database session. That is not a
convenience; it is the whole safety argument ``reporting.tokens``' module docstring makes: an override
Theme's payload becomes part of a CONFIDENTIAL client deliverable, so it gets the identical closed
Token allowlist and font-face grammar a bundled Theme is held to, never a looser or differently-shaped
check invented for "the admin form case". A submitted TOML that fails ANY part of that schema is
refused with the parser's own ``ThemeFileError`` message and NOTHING is stored — no partial row, no
half-applied edit (mirrors ``validate_tokens``'s own wholesale-reject rule one layer up).

## Where a Theme's NAME comes from

A bundled Theme's identity is anchored to its filename (``theme_files._parse_theme_toml`` raises if
``[identity].name`` disagrees with it). An override Theme has no filename to anchor to, so this module
uses the identical anchoring rule against the TOML's OWN declared ``[identity].name`` instead of asking
an operator to type a name in a second, independent form field that could then drift from the file's
own content: :func:`_peek_identity_name` reads ``[identity].name`` straight out of the submitted text
and that becomes the ``expected_name`` fed back into ``_parse_theme_toml``. There is exactly one place
a Theme's name is decided — the TOML itself — the same as a bundled file.

## Name collisions — bundled and installed both win

A submitted name that matches a BUNDLED or INSTALLED Theme is refused outright (``_collision_reason``):
those two Provenances are code (a Theme the operator/firm shipped or installed deliberately), and an
override — data a form accepted — must never be able to silently shadow one. This mirrors
``reporting.theme_discovery``'s own collision policy for an installed-vs-bundled name clash; this module
is simply the third leg of the same rule.

## Delete/rename must never orphan the per-install default

The per-install default (:class:`~scribble.models.ScribbleSettings`) stores a bare Theme NAME, not a
foreign key — see that model's docstring for why. That means deleting or renaming the override Theme it
currently names would otherwise leave it pointing at nothing, silently, with no error until someone
much later wonders why the "branded" deliverable came out unbranded. Both the delete and the (rename)
update route therefore actively re-point or clear that setting in the SAME transaction as the mutation
that would otherwise orphan it — see ``_delete_theme_override``/``_update_theme_override`` below. There
is, as yet, no PER-ENGAGEMENT Theme reference in this codebase to worry about the same way (the
``Engagement.report_theme`` column this ticket's investigation found was cut in ``75159ed`` and has not
returned) — when it does, its reader is expected to lean on ``reporting.themes.get_theme``'s existing
"unknown name falls back rather than raises" contract, exactly as it already does for an untrusted
``?theme=`` query value, rather than needing its own cleanup pass here.

## Raster-only Marks — a reminder, not an enforcement point

``theme_files._parse_theme_toml`` accepts (but does not yet read) a reserved ``[marks]`` table, so an
operator's submitted TOML MAY already contain one, and this module stores it verbatim as part of
``source_toml`` — it has no opinion on Marks at all. See ``scribble.models.ScribbleThemeOverride``'s
docstring for the enforcement point that must exist before that table is ever READ:
``reporting.marks.resolve_mark``, whose ``_SVG_ALLOWED_PROVENANCES`` already excludes ``"override"``.
Nothing below this line is a substitute for that gate.

## Admin gate

Every MUTATING route below requires :func:`_actor_is_admin` — a Theme rewrites the letterhead of every
future deliverable this install produces, the same "branding is admin-only" argument
``cream/cream/api.py``'s ``current_actor_is_admin()`` gate makes for CREAM's ``Brand``. Scribble's own
``scribble/deps.py`` does not yet carry that helper the way ``cream/deps.py``/``vector/deps.py`` do —
adding it there is out of this module's file ownership for this ticket (``deps.py`` is an orchestrator
integration point) — so :func:`_actor_is_admin` reproduces the IDENTICAL semantics locally against
``scribble.deps.current_actor()`` (which does already exist): standalone (no host actor hook at all)
resolves to admin (a single local user has no one else to defer to), and a mounted host's actor is
admin per its OWN role, checked the way ``tests/conftest.py``'s ``StubHost.can_view_client`` already
checks a lotek ``UserRole`` (a callable ``role.is_admin()`` when present — matching ``app/models.py``'s
real shape — falling back to a bare string compare otherwise). Whoever eventually adds
``current_actor_is_admin()`` to ``scribble/deps.py`` should have this function delegate to it and
delete the duplicate; the two must never disagree in the meantime, which is why this one is written
against the SAME actor/role shape rather than inventing its own.

Read routes (list/page) are deliberately NOT admin-gated — like ``assessment_types_ui.py``'s list view,
seeing which Themes exist and which Provenance each carries is not itself sensitive, only changing one
is.
"""

from __future__ import annotations

import tomllib
from typing import Any

from flask import jsonify, render_template, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from scribble.deps import current_actor, current_actor_username, get_config, open_session
from scribble.models import ScribbleSettings, ScribbleThemeOverride
from scribble.reporting import themes as reporting_themes
from scribble.reporting.theme_discovery import discover_installed_themes
from scribble.reporting.theme_files import (
    ThemeFileError,
    _parse_theme_toml,
    list_theme_files,
    load_theme_file,
)

_REGISTERED = False

# Sane ceiling on a submitted/edited Theme's raw TOML text, enforced BEFORE it ever reaches tomllib or a
# DB session — the same "reject cheaply, before anything expensive" shape
# `reporting.theme_files.MAX_EMBEDDED_FONT_BYTES` uses for font payloads. A real Theme (a few hex
# colours, three font stacks, a handful of @font-face declarations) is a few KB; 200 KB is generous
# headroom, not a working ceiling anyone should approach, and it bounds the size of every audit row this
# module writes too (the full text is what `_audit_snapshot` records).
MAX_THEME_TOML_CHARS = 200_000


# --- admin gate --------------------------------------------------------------------------------------


def _actor_is_admin() -> bool:
    """See the module docstring's "Admin gate" section for why this exists here rather than importing
    a `current_actor_is_admin()` scribble.deps does not yet have."""
    actor = current_actor()
    if actor is None:
        # No actor at all: either standalone (no host `current_actor` hook -> single local user, treat
        # as admin, same fail-open-when-standalone rule cream/vector use), or a mounted host whose hook
        # answered "nobody is logged in" (fail CLOSED -- a Theme mutation must not run unattributed).
        try:
            return get_config().extras.get("current_actor") is None
        except RuntimeError:  # pragma: no cover - defensive; no app context
            return True
    role = getattr(actor, "role", None)
    if role is None:
        return False
    try:
        if hasattr(role, "is_admin"):
            return bool(role.is_admin())
        return str(role).strip().lower() == "admin"
    except Exception:  # noqa: BLE001 - a misbehaving role object must fail closed, never widen access
        return False


def _require_admin():
    """`None` if the caller is admin; otherwise the (response, 403) to return immediately."""
    if _actor_is_admin():
        return None
    return jsonify(
        ok=False,
        error="admin only: a report Theme sets the branding of every future deliverable",
    ), 403


# --- known-name bookkeeping (bundled / installed / override, merged) --------------------------------


def _bundled_names() -> frozenset[str]:
    return frozenset(list_theme_files())


def _installed_descriptors():
    """`{name: InstalledThemeDescriptor}` from the real environment. `discover_installed_themes()`
    documents that discovery itself never raises (a broken package becomes a collected error, not an
    exception), so this is not wrapped defensively -- see reporting/theme_discovery.py."""
    return discover_installed_themes()


def _known_theme_names(db, discovery) -> frozenset[str]:
    """Every Theme NAME this install could currently resolve a Theme selection against -- the closed
    set `set_default_theme` validates a submitted default against. Includes the bare stamp names
    (`"auto"`/`"light"`/`"dark"` -- `reporting.themes.list_themes()`) alongside every bundled, installed,
    and override Theme name, because the per-install default setting is a plain string that
    `reporting.themes.get_theme()` (the orchestrator's read side) resolves the same way it resolves an
    untrusted `?theme=` value -- either registry is a legal value for it to carry.
    """
    names = {t.name for t in reporting_themes.list_themes()}
    names |= _bundled_names()
    names |= set(discovery.themes)
    names |= set(db.scalars(select(ScribbleThemeOverride.name)).all())
    return frozenset(names)


def _collision_reason(db, name: str, discovery, *, exclude_id=None) -> str | None:
    """Why `name` may NOT be used for an override Theme, or `None` if it is free.

    Bundled and installed Themes are CODE (something the operator/firm shipped or installed
    deliberately); an override is data a form accepted. Both win over an override with the same name --
    see the module docstring's "Name collisions" section.
    """
    if name in _bundled_names():
        return f"'{name}' is a bundled Theme name — bundled Themes always win; choose a different name"
    if name in discovery.themes:
        return (
            f"'{name}' is an installed Theme name — installed Themes win over an override; "
            "choose a different name"
        )
    stmt = select(ScribbleThemeOverride).where(ScribbleThemeOverride.name == name)
    if exclude_id is not None:
        stmt = stmt.where(ScribbleThemeOverride.id != exclude_id)
    if db.scalar(stmt) is not None:
        return f"an override Theme named '{name}' already exists — edit it instead of creating a new one"
    return None


# --- parsing / validation -------------------------------------------------------------------------------


def _peek_identity_name(text: str) -> str:
    """Best-effort `[identity].name` out of `text`, or `""` if the TOML is malformed / lacks one.

    Only ever used to seed `expected_name` for `_parse_theme_toml` below -- never trusted on its own.
    When this returns `""` because the text is genuinely broken, `_parse_theme_toml("", text)` still
    raises the RIGHT, detailed `ThemeFileError` (missing `[identity]`, non-string `name`, ...) before it
    ever reaches the "name does not match" check that `""` would otherwise trip spuriously.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ""
    identity = data.get("identity")
    if not isinstance(identity, dict):
        return ""
    name = identity.get("name")
    return name if isinstance(name, str) else ""


def _extract_source_toml() -> tuple[str | None, Any]:
    """The submitted TOML text from either a multipart `file` upload or a JSON `source_toml` field, or
    `(None, <error response>)`. Mirrors `artifacts_api.create_artifact`'s "multipart file OR JSON body"
    shape, the established convention for a route that accepts either.
    """
    upload = request.files.get("file")
    if upload is not None:
        raw = upload.read()
        if len(raw) > MAX_THEME_TOML_CHARS:
            return None, (
                jsonify(ok=False, error=f"Theme file too large (max {MAX_THEME_TOML_CHARS} bytes)"),
                400,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, (jsonify(ok=False, error="Theme file must be UTF-8 text"), 400)
    else:
        payload = request.get_json(silent=True) or {}
        candidate = payload.get("source_toml")
        if not isinstance(candidate, str) or not candidate.strip():
            return None, (
                jsonify(ok=False, error="source_toml (or a multipart 'file' upload) is required"),
                400,
            )
        if len(candidate) > MAX_THEME_TOML_CHARS:
            return None, (
                jsonify(ok=False, error=f"Theme TOML too large (max {MAX_THEME_TOML_CHARS} characters)"),
                400,
            )
        text = candidate
    return text, None


# --- rendering rows for the admin list -----------------------------------------------------------------


def _audit_snapshot(row: ScribbleThemeOverride) -> dict[str, Any]:
    return {"name": row.name, "label": row.label, "source_toml": row.source_toml}


def _override_out(row: ScribbleThemeOverride, *, default_name: str | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "label": row.label,
        "provenance": "override",
        "source_toml": row.source_toml,
        "is_default": row.name == default_name,
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _list_all_themes(db, discovery, *, default_name: str | None) -> list[dict[str, Any]]:
    """Every Theme this install knows about, across all three Provenances, for the admin list. A broken
    bundled or installed Theme is shown WITH its error rather than raised — see the module the two
    loaders come from for why a broken Theme must never be silently absent (`75159ed`'s "fail loudly")
    nor allowed to crash the one page an operator would use to notice and fix it.
    """
    rows: list[dict[str, Any]] = []
    for name in list_theme_files():
        try:
            theme_file = load_theme_file(name)
        except ThemeFileError as exc:
            rows.append({
                "name": name, "label": name, "provenance": "bundled",
                "error": str(exc), "is_default": name == default_name,
            })
            continue
        if theme_file is None:  # pragma: no cover - list_theme_files() only names files that exist
            continue
        rows.append({
            "name": theme_file.name, "label": theme_file.label, "provenance": "bundled",
            "error": None, "is_default": theme_file.name == default_name,
        })
    for descriptor in discovery.themes.values():
        label = descriptor.name
        error = None
        try:
            theme_file = _parse_theme_toml(descriptor.name, descriptor.load_toml())
            label = theme_file.label
        except Exception as exc:  # noqa: BLE001 - render this row's problem, never crash the admin page
            error = str(exc)
        rows.append({
            "name": descriptor.name, "label": label, "provenance": "installed",
            "error": error, "is_default": descriptor.name == default_name,
            "distribution": descriptor.distribution,
        })
    for override_row in db.scalars(select(ScribbleThemeOverride).order_by(ScribbleThemeOverride.name)):
        rows.append({**_override_out(override_row, default_name=default_name), "error": None})
    return rows


def _get_or_create_settings(db) -> ScribbleSettings:
    settings = db.scalar(select(ScribbleSettings).where(ScribbleSettings.slot == "default"))
    if settings is None:
        settings = ScribbleSettings(slot="default")
        db.add(settings)
        db.flush()
    return settings


def register(api_bp, bp) -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # Local import: `api_pat.py` is the machine-API module and pulls in `scribble.host`/`scribble.authz`/
    # `scribble.artifacts_api`/... at ITS module scope. Importing `_audit` from it at THIS module's top
    # level would make every one of those load merely to reach a cookie-authed admin CRUD surface with
    # no other relationship to the machine API -- deferred the same way `scribble.deps.client_model`
    # defers its own `scribble.models` import "to avoid an import cycle".
    from scribble.api_pat import _audit

    # ------------------------------------------------------------------------------- UI (bp)

    @bp.get("/themes", endpoint="themes_library")
    def themes_library():
        with open_session() as db:
            settings = db.scalar(select(ScribbleSettings).where(ScribbleSettings.slot == "default"))
            default_name = settings.default_report_theme if settings else None
            discovery = _installed_descriptors()
            rows = _list_all_themes(db, discovery, default_name=default_name)
            known_names = sorted(_known_theme_names(db, discovery))
        return render_template(
            "scribble/themes.html",
            rows=rows,
            default_name=default_name or "",
            known_names=known_names,
            is_admin=_actor_is_admin(),
            discovery_errors=discovery.errors,
            discovery_collisions=discovery.collisions,
        )

    # ------------------------------------------------------------------------------- JSON (api_bp)

    @api_bp.get("/themes")
    def list_themes():
        with open_session() as db:
            settings = db.scalar(select(ScribbleSettings).where(ScribbleSettings.slot == "default"))
            default_name = settings.default_report_theme if settings else None
            discovery = _installed_descriptors()
            rows = _list_all_themes(db, discovery, default_name=default_name)
            return jsonify(ok=True, themes=rows, default_report_theme=default_name)

    @api_bp.post("/themes")
    def create_theme_override():
        denied = _require_admin()
        if denied:
            return denied
        text, err = _extract_source_toml()
        if err:
            return err
        expected_name = _peek_identity_name(text)
        try:
            parsed = _parse_theme_toml(expected_name, text)
        except ThemeFileError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        with open_session() as db:
            discovery = _installed_descriptors()
            collision = _collision_reason(db, parsed.name, discovery)
            if collision:
                return jsonify(ok=False, error=collision), 409
            username = current_actor_username()
            row = ScribbleThemeOverride(
                # Stored CASE-FOLDED. `theme_registry.resolve_theme` lower-cases the requested name and
                # the switcher renders the folded name as its `<option value>`, so a row stored as
                # `Acme` was offered as `acme` and then failed to resolve -- silently falling back to
                # `auto`, i.e. a Theme that shows up in the list and does nothing when picked.
                name=parsed.name.strip().lower(), label=parsed.label, source_toml=text,
                created_by=username, updated_by=username,
            )
            db.add(row)
            db.flush()
            _audit(
                db, "create_override_theme", subject_type="scribble_theme_override",
                subject_id=row.id, before=None, after=_audit_snapshot(row),
            )
            current_default = _get_or_create_settings(db).default_report_theme
            try:
                db.commit()
            except IntegrityError:
                # `_collision_reason` checked the name before this commit, so two concurrent admins
                # naming the same new Theme both pass that check and the loser lands here. The UNIQUE
                # constraint is the real arbiter; this just reports the loss as the conflict it is
                # rather than a 500. (Adversarial review of #113.)
                db.rollback()
                return jsonify(ok=False, error="a Theme with that name already exists"), 409
            return jsonify(ok=True, theme=_override_out(row, default_name=current_default)), 201

    @api_bp.post("/themes/<uuid:theme_id>")
    def update_theme_override(theme_id):
        denied = _require_admin()
        if denied:
            return denied
        text, err = _extract_source_toml()
        if err:
            return err
        expected_name = _peek_identity_name(text)
        try:
            parsed = _parse_theme_toml(expected_name, text)
        except ThemeFileError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        with open_session() as db:
            row = db.get(ScribbleThemeOverride, theme_id)
            if row is None:
                return jsonify(ok=False, error="not found"), 404
            discovery = _installed_descriptors()
            if parsed.name != row.name:
                collision = _collision_reason(db, parsed.name, discovery, exclude_id=row.id)
                if collision:
                    return jsonify(ok=False, error=collision), 409
            before = _audit_snapshot(row)
            old_name = row.name
            row.name = parsed.name.strip().lower()  # canonical, same reason as create
            row.label = parsed.label
            row.source_toml = text
            row.updated_by = current_actor_username()
            db.flush()
            if old_name != row.name:
                # Renaming the Theme the install default currently names must not orphan that
                # setting -- see the module docstring's "Delete/rename must never orphan" section.
                settings = _get_or_create_settings(db)
                if settings.default_report_theme == old_name:
                    settings.default_report_theme = row.name
                    db.flush()
            _audit(
                db, "update_override_theme", subject_type="scribble_theme_override",
                subject_id=row.id, before=before, after=_audit_snapshot(row),
            )
            # Read the ACTUAL default rather than passing None. Hardcoding it reported is_default=false
            # even right after renaming the Theme that is the current default -- and the rename branch
            # above deliberately carries the default across, so the response contradicted the write
            # that had just happened. The UI reads this field to mark the active Theme.
            current_default = _get_or_create_settings(db).default_report_theme
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return jsonify(ok=False, error="a Theme with that name already exists"), 409
            return jsonify(ok=True, theme=_override_out(row, default_name=current_default))

    @api_bp.post("/themes/<uuid:theme_id>/delete")
    def delete_theme_override(theme_id):
        denied = _require_admin()
        if denied:
            return denied
        with open_session() as db:
            row = db.get(ScribbleThemeOverride, theme_id)
            if row is None:
                return jsonify(ok=False, error="not found"), 404
            before = _audit_snapshot(row)
            name = row.name
            db.delete(row)
            db.flush()
            # Deleted Theme was the install default -> clear it (falls back further to
            # reporting.themes.DEFAULT_THEME) rather than leaving a dangling name. See the module
            # docstring's "Delete/rename must never orphan" section for the full reasoning, including
            # why a per-ENGAGEMENT reference needs no equivalent cleanup here (none exists yet).
            settings = _get_or_create_settings(db)
            if settings.default_report_theme == name:
                settings.default_report_theme = None
                db.flush()
            _audit(
                db, "delete_override_theme", subject_type="scribble_theme_override",
                subject_id=row.id, before=before, after=None,
            )
            db.commit()
            return jsonify(ok=True)

    @api_bp.post("/themes/default")
    def set_default_theme():
        denied = _require_admin()
        if denied:
            return denied
        payload = request.get_json(silent=True) or {}
        raw_name = payload.get("name")
        name = (raw_name or "").strip()
        with open_session() as db:
            discovery = _installed_descriptors()
            if name and name not in _known_theme_names(db, discovery):
                return jsonify(ok=False, error=f"unknown Theme '{name}'"), 400
            settings = _get_or_create_settings(db)
            before = {"default_report_theme": settings.default_report_theme}
            settings.default_report_theme = name or None
            db.flush()
            _audit(
                db, "set_default_theme", subject_type="scribble_theme_settings",
                subject_id=settings.id, before=before,
                after={"default_report_theme": settings.default_report_theme},
            )
            db.commit()
            return jsonify(ok=True, default_report_theme=settings.default_report_theme)
