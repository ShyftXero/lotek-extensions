"""AssessmentType admin UI + API (WS13 owns this module).

`AssessmentType` is a **user-managed lookup**, not a hardcoded enum — the seed ships four defaults
(Internal / External / Web App / Device-Mobile, see `fraction/seed/loader.py::_DEFAULT_TYPES`) but users
must be able to add, rename, recolor, reorder, deactivate, and (when safe) delete their own types. This
module is the admin surface for that table.

Routes
------
UI (``bp``):
    GET  /assessment-types                 list ALL types (active + inactive), edit-in-place rows,
                                            a "new type" form, and a live `FindingGroup` reference count
                                            per type.
JSON (``api_bp``):
    POST /assessment-types                 create (name required; slug auto-derived from name if
                                            omitted/blank; unique name/slug enforced -> 400 on conflict)
    POST /assessment-types/<id>            update in place (edit-in-place; id never changes) — any of
                                            name/slug/color/default_order/active; renaming/recoloring/
                                            reordering/(de)activating are all just partial updates here
    POST /assessment-types/<id>/delete     hard delete — refused (400, row untouched) if any
                                            `FindingGroup` still references this type; the JSON error
                                            tells the caller to deactivate instead. Deactivation is just
                                            `POST /assessment-types/<id>` with `{"active": false}`.

Contract: expose `def register(api_bp, bp) -> None` (idempotent) that adds routes to `bp` (UI) and
`api_bp` (JSON), mirroring the WS2/WS3 hook shape (`docs/RAILS.md` §1). Keep the `assessment_types`
endpoint name — the driver adds a sidebar link to it (see `docs/_patches/ws13-assessment-types.md`).
This module does NOT edit `fraction/__init__.py`; the driver wires `register` into
`_wire_feature_routes` there.

Referential integrity: `FindingGroup.assessment_type_id` is nullable but we never null it out from here
— delete is refused outright while any group still points at the row, so a group's `assessment_type_id`
is never silently orphaned by this admin UI. (A group whose type is later deactivated keeps pointing at
it; deactivating only hides the type from "active" pickers elsewhere, it does not touch existing groups.)
"""

from __future__ import annotations

import re
from typing import Any

from flask import jsonify, render_template, request, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from fraction.deps import open_session
from fraction.models import AssessmentType, FindingGroup

_REGISTERED = False


def register(api_bp, bp) -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    # ------------------------------------------------------------------------------- UI (bp)

    @bp.get("/assessment-types", endpoint="assessment_types")
    def assessment_types():
        with open_session() as db:
            types = db.scalars(
                select(AssessmentType).order_by(AssessmentType.default_order, AssessmentType.name)
            ).all()
            counts = dict(
                db.execute(
                    select(FindingGroup.assessment_type_id, func.count(FindingGroup.id)).group_by(
                        FindingGroup.assessment_type_id
                    )
                ).all()
            )
            rows = [(t, counts.get(t.id, 0)) for t in types]
            return render_template("fraction/assessment_types.html", rows=rows)

    # ------------------------------------------------------------------------------- JSON (api_bp)

    @api_bp.post("/assessment-types")
    def create_assessment_type():
        payload = request.get_json(silent=True) or {}
        name = (payload.get("name") or "").strip()
        if not name:
            return jsonify(ok=False, error="name is required"), 400

        raw_slug = (payload.get("slug") or "").strip()
        slug = _slugify(raw_slug) if raw_slug else _slugify(name)
        color, color_ok = _normalize_color(payload.get("color"))
        if not color_ok:
            return jsonify(ok=False, error=_COLOR_ERROR), 400
        default_order = _as_int(payload.get("default_order"))
        if default_order is None:
            default_order = 0

        with open_session() as db:
            conflict = _find_conflict(db, name=name, slug=slug)
            if conflict is not None:
                return jsonify(ok=False, error=conflict), 400

            at = AssessmentType(
                name=name, slug=slug, color=color, default_order=default_order, active=True
            )
            db.add(at)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return jsonify(ok=False, error="name or slug already in use"), 400
            return (
                jsonify(
                    ok=True,
                    id=at.id,
                    redirect=url_for("fraction.assessment_types"),
                ),
                201,
            )

    @api_bp.post("/assessment-types/<int:type_id>")
    def update_assessment_type(type_id: int):
        payload = request.get_json(silent=True) or {}
        with open_session() as db:
            at = db.get(AssessmentType, type_id)
            if at is None:
                return jsonify(ok=False, error="assessment type not found"), 404

            new_name = at.name
            new_slug = at.slug

            if "name" in payload:
                new_name = (payload.get("name") or "").strip()
                if not new_name:
                    return jsonify(ok=False, error="name cannot be empty"), 400

            if "slug" in payload:
                raw_slug = (payload.get("slug") or "").strip()
                new_slug = _slugify(raw_slug) if raw_slug else _slugify(new_name)

            if new_name != at.name or new_slug != at.slug:
                conflict = _find_conflict(db, name=new_name, slug=new_slug, exclude_id=type_id)
                if conflict is not None:
                    return jsonify(ok=False, error=conflict), 400

            # Validate color BEFORE mutating the row, so a bad color rejects without partial changes.
            if "color" in payload:
                color, color_ok = _normalize_color(payload.get("color"))
                if not color_ok:
                    return jsonify(ok=False, error=_COLOR_ERROR), 400
                at.color = color

            at.name = new_name
            at.slug = new_slug

            if "default_order" in payload:
                order = _as_int(payload.get("default_order"))
                if order is not None:
                    at.default_order = order
            if "active" in payload:
                at.active = bool(payload.get("active"))

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return jsonify(ok=False, error="name or slug already in use"), 400
            return jsonify(ok=True, id=at.id)

    @api_bp.post("/assessment-types/<int:type_id>/delete")
    def delete_assessment_type(type_id: int):
        with open_session() as db:
            at = db.get(AssessmentType, type_id)
            if at is None:
                return jsonify(ok=False, error="assessment type not found"), 404

            name = at.name  # captured before any rollback expires the instance

            # Read the reference count in THIS session, immediately before deleting, so the check is
            # as close to the write as possible. This gives the caller a clear, count-bearing error in
            # the normal case, but it is a check-then-act (TOCTOU): a group could be pointed at this
            # type between here and the commit. The IntegrityError guard below is the real backstop.
            in_use = _reference_count(db, type_id)
            if in_use:
                return (
                    jsonify(
                        ok=False,
                        error=(
                            f"cannot delete {name!r}: {in_use} finding group(s) still reference it; "
                            "deactivate it instead"
                        ),
                        in_use=in_use,
                    ),
                    400,
                )

            # Backstop for the TOCTOU race the pre-check can't close, AND for a host DB that enforces
            # the FK (e.g. SQLite with ``PRAGMA foreign_keys=ON``, or Postgres): the commit's DELETE is
            # rejected rather than silently orphaning a ``FindingGroup.assessment_type_id``. Without
            # this, an FK-enforcing DB would surface a 500 and a non-enforcing one would orphan the FK
            # — exactly what this module promises never to do. Mirrors create/update's IntegrityError
            # handling. The ``FindingGroup`` row is untouched (nothing referencing it was mutated).
            try:
                db.delete(at)
                db.commit()
            except IntegrityError:
                db.rollback()
                return (
                    jsonify(
                        ok=False,
                        error=(
                            f"cannot delete {name!r}: still referenced by a finding group; "
                            "deactivate it instead"
                        ),
                    ),
                    400,
                )
            return jsonify(ok=True, deleted=True)


# --------------------------------------------------------------------------------------- helpers

# A color is stored verbatim and flows into ``style="background:{color}"`` in the rendered report
# (WS7/WS8). Free text there is CSS injection (`;`/`:`/`url(...)` are not escaped by the report), so
# the server enforces a strict hex shape here — the ``<input type="color">`` picker is only a client
# hint. 3–8 hex digits covers #rgb / #rrggbb / #rgba / #rrggbbaa.
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_COLOR_ERROR = "color must be a hex value like #7c3aed"


def _normalize_color(value: Any) -> tuple[str | None, bool]:
    """Return ``(color_or_none, ok)``.

    An empty/absent color is valid and stored as ``None`` (``(None, True)``). A non-empty value must
    match ``_COLOR_RE`` or it is rejected (``(None, False)``) — callers turn that into a 400 rather
    than storing attacker-controlled CSS.
    """
    raw = (value or "").strip() if isinstance(value, str) else ("" if value is None else str(value))
    if not raw:
        return None, True
    if _COLOR_RE.match(raw):
        return raw, True
    return None, False


def _reference_count(db, type_id: int) -> int:
    """How many ``FindingGroup`` rows point at this assessment type (in ``db``'s session)."""
    return (
        db.scalar(
            select(func.count())
            .select_from(FindingGroup)
            .where(FindingGroup.assessment_type_id == type_id)
        )
        or 0
    )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "assessment-type"


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_conflict(db, *, name: str, slug: str, exclude_id: int | None = None) -> str | None:
    """Return a human-readable error if `name`/`slug` collide with another row, else None.

    Case-insensitive on both columns (SQLite's default TEXT collation is case-sensitive, so an
    exact-match check alone would let "Internal" and "internal" both through and then hit an
    IntegrityError only for a truly identical string) — belt-and-suspenders with the model's
    `unique=True` columns, which still catch anything this misses (race conditions) via the
    IntegrityError fallback in the callers.
    """
    name_stmt = select(AssessmentType).where(func.lower(AssessmentType.name) == name.lower())
    slug_stmt = select(AssessmentType).where(func.lower(AssessmentType.slug) == slug.lower())
    if exclude_id is not None:
        name_stmt = name_stmt.where(AssessmentType.id != exclude_id)
        slug_stmt = slug_stmt.where(AssessmentType.id != exclude_id)

    if db.scalar(name_stmt) is not None:
        return f"an assessment type named {name!r} already exists"
    if db.scalar(slug_stmt) is not None:
        return f"slug {slug!r} is already in use"
    return None
