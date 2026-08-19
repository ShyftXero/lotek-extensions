"""Artifact JSON API routes (WS5, PLAN.md §7, plans/CONTRACTS.md #6 & ownership map).

Exposes :func:`register`, which the driver calls once from ``scribble/__init__.py`` — after the API
(``api_bp``) and UI (``bp``) blueprints exist but *before* they're registered on the host Flask app — to
attach the artifact routes:

    from scribble.artifacts_api import register as register_artifacts
    register_artifacts(api_bp, bp)

This module deliberately does not import or edit ``scribble/api.py`` / ``scribble/blueprint.py`` /
``scribble/__init__.py`` (frozen/shared files); it only adds routes to the blueprint objects handed to
it, exactly like ``scribble/blueprint.py`` route functions do with ``@bp.get(...)``.

``register`` is idempotent: calling it more than once with the same blueprint object (e.g. once per test
app created in the same process, or if the host calls it defensively) only registers the routes once —
Flask blueprints replay every ``@bp.route`` ever recorded each time ``app.register_blueprint()`` runs, so
without the guard a second call would re-append the same endpoints and blow up with "View function
mapping is overwriting an existing endpoint function" the next time a *new* app registers the blueprint.

Routes (all on ``api_bp``, i.e. mounted at ``<url_prefix>/api``):
    POST   /artifacts                                 create (multipart ``file=`` OR JSON base64 blob)
    GET    /artifacts/<id>/raw                         stream the file (forced ``attachment``)
    POST   /artifacts/<id>                             update caption / include_in_report / kind
    POST   /artifacts/<id>/delete                      delete row + on-disk file
    GET    /engagements/<engagement_id>/artifacts       list every artifact on the engagement (ext#51)
    GET    /findings/<finding_id>/artifacts            list, ordered by order_index
    POST   /findings/<finding_id>/artifacts/reorder    body ``{"order": [id, ...]}`` -> order_index

``POST /artifacts`` also accepts:
    ``placement``       -- ``ArtifactPlacement`` value (``attached`` default, or ``inline``); invalid ->
                            400. Same form/JSON field as ``kind``.
    ``idempotency_key``  -- dedup token for retried uploads (the offline upload-outbox story, PLAN.md
                            §19), read from a form/JSON field of that name or the ``Idempotency-Key``
                            request header (field wins if both given). If a matching ``Artifact`` already
                            exists for ``(engagement_id, idempotency_key)`` it's returned as-is with 200
                            instead of creating a second row/file; this is a query-based check, not a DB
                            unique constraint (see ``Artifact.idempotency_key`` in scribble/models.py).
    ``finding_id``        -- optional. Validated against ``engagement_id`` (ext#40 mechanism 3): a
                            ``finding_id`` naming a finding on a DIFFERENT engagement is silently nulled
                            rather than stored verbatim. The 200/201 response echoes the EFFECTIVE
                            ``finding_id`` (``null`` if dropped) plus a ``finding_id_dropped`` bool so a
                            well-behaved caller can tell its request was accepted but not attached where
                            it asked.
"""

from __future__ import annotations

import base64
import binascii
import re

from flask import jsonify, request, send_file, url_for

from scribble.artifacts_storage import delete_file, guess_content_type, resolve_path, save_bytes
from scribble.authz import can_view_engagement
from scribble.deps import current_actor, current_actor_username, get_config, open_session
from scribble.enums import ArtifactKind, ArtifactPlacement
from scribble.models import Artifact, Engagement, EngagementFinding

_REGISTERED_ATTR = "_scribble_artifacts_registered"


def artifact_url(artifact_id: int) -> str:
    """Build the raw-file URL for an artifact. Usable by the report context (WS7) as ``artifact_url``.

    Requires an active Flask app/request context (true for every view function, including the render
    routes that build a ``ReportContext``).
    """
    return url_for("scribble_api.artifact_raw", artifact_id=artifact_id)


def _infer_kind(content_type: str | None) -> ArtifactKind:
    if content_type:
        if content_type.startswith("image/"):
            return ArtifactKind.screenshot
        if content_type.startswith("text/"):
            return ArtifactKind.text
    return ArtifactKind.file


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_INT_RE = re.compile(r"[0-9]+")
_MAX_FINDING_ID = 2**31 - 1


def _finding_id_or_400(raw):
    """``(finding_id, refusal)`` for a caller-supplied ``finding_id`` — the cookie counterpart of
    ``api_pat._finding_id_or_400`` (ext#52). Exactly one of the pair is non-None.

    Absent/empty means "engagement-level evidence" (a legitimate request — the multipart surface
    submits ``finding_id=""`` for an untouched field). Anything else that is not a whole number in
    range is refused with a 400 rather than silently coerced/dropped: a float (``2.9``) or a bool
    (``True`` is an ``int`` subclass) would otherwise attach to a finding the caller never named, and
    reading "did you ask for one" off the response would then be a lie. See ``api_pat``'s docstring
    for the full reasoning; this mirrors it exactly so the cookie and PAT surfaces agree.
    """
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, bool):
        return None, (jsonify(error=f"invalid finding_id {raw!r}"), 400)
    if isinstance(raw, int):
        fid = raw
    elif isinstance(raw, str) and _INT_RE.fullmatch(raw.strip()):
        fid = int(raw.strip())
    else:
        return None, (jsonify(error=f"invalid finding_id {raw!r}"), 400)
    if not 0 < fid <= _MAX_FINDING_ID:
        return None, (jsonify(error=f"invalid finding_id {raw!r}"), 400)
    return fid, None


def _artifact_dict(a: Artifact) -> dict:
    return {
        "id": a.id,
        "engagement_id": a.engagement_id,
        "finding_id": a.finding_id,
        "kind": a.kind.value,
        "placement": a.placement.value,
        "filename": a.filename,
        "content_type": a.content_type,
        "byte_size": a.byte_size,
        "sha256": a.sha256,
        "caption": a.caption or "",
        "order_index": a.order_index,
        "include_in_report": a.include_in_report,
        "url": artifact_url(a.id),
        "update_url": url_for("scribble_api.update_artifact", artifact_id=a.id),
        "delete_url": url_for("scribble_api.delete_artifact", artifact_id=a.id),
    }


def register(api_bp, bp) -> None:  # noqa: ARG001 - `bp` reserved for future UI routes, kept per contract
    """Attach artifact routes to ``api_bp`` (JSON API). ``bp`` (the UI blueprint) isn't used today — the
    gallery is a template partial included by whatever page owns the finding (WS3), not a standalone
    page — but is accepted per the frozen ``register(api_bp, bp)`` hook signature."""
    if getattr(api_bp, _REGISTERED_ATTR, False):
        return
    setattr(api_bp, _REGISTERED_ATTR, True)

    @api_bp.post("/artifacts")
    def create_artifact():
        cfg = get_config()
        caption: str | None
        kind_raw: str | None
        placement_raw: str | None
        idempotency_key: str | None

        upload = request.files.get("file")
        if upload is not None:
            engagement_id = _as_int(request.form.get("engagement_id"))
            finding_id, bad_finding_id = _finding_id_or_400(request.form.get("finding_id"))
            if bad_finding_id is not None:
                return bad_finding_id
            caption = request.form.get("caption")
            kind_raw = request.form.get("kind")
            placement_raw = request.form.get("placement")
            idempotency_key = request.form.get("idempotency_key")
            filename = upload.filename or "artifact"
            data = upload.read()
        elif request.is_json:
            payload = request.get_json(silent=True) or {}
            engagement_id = _as_int(payload.get("engagement_id"))
            finding_id, bad_finding_id = _finding_id_or_400(payload.get("finding_id"))
            if bad_finding_id is not None:
                return bad_finding_id
            caption = payload.get("caption")
            kind_raw = payload.get("kind")
            placement_raw = payload.get("placement")
            idempotency_key = payload.get("idempotency_key")
            filename = payload.get("filename") or "artifact"
            content_b64 = (
                payload.get("content_base64") or payload.get("data_base64") or payload.get("data")
            )
            if not content_b64:
                return jsonify(error="content_base64 is required"), 400
            try:
                data = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError):
                return jsonify(error="invalid base64 content"), 400
        else:
            return jsonify(error="expected a multipart file upload or a JSON body"), 400

        if engagement_id is None:
            return jsonify(error="engagement_id is required"), 400
        if not data:
            return jsonify(error="empty upload"), 400

        # Tenancy (read the target BEFORE writing anything): unlike every other engagement-scoped
        # route in this package, ``engagement_id`` here comes from the request BODY, not the URL, so
        # the blueprint-wide before_request gate (scribble/authz.py, keyed on view args) structurally
        # cannot reach it. Without this, any authenticated actor could attach evidence to another
        # client's engagement just by naming its id in the upload. Checked before ``save_bytes`` writes
        # a single byte to disk.
        #
        # Byte-identical refusal for "no such engagement" and "exists but not visible to this actor":
        # the aborting ``authorize_engagement_view`` would answer the second case with Flask's default
        # HTML 404 page, distinguishable from this route's own JSON 404 above -- a minor existence
        # oracle (adversarial review on #256). ``can_view_engagement`` is the same predicate, called
        # explicitly so both cases return this route's own JSON shape.
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None or not can_view_engagement(engagement, current_actor()):
                return jsonify(error="engagement not found"), 404

        # Idempotency guard (PLAN.md §19 offline upload-outbox): fall back to the request header when no
        # form/JSON field was given. Checked BEFORE save_bytes() writes anything to disk, so a retried
        # upload with the same key never creates a second file OR a second row -- it just returns the
        # original row's response shape with 200 instead of 201.
        if not idempotency_key:
            idempotency_key = request.headers.get("Idempotency-Key")
        idempotency_key = idempotency_key or None
        if idempotency_key:
            with open_session() as db:
                existing = (
                    db.query(Artifact)
                    .filter(
                        Artifact.engagement_id == engagement_id,
                        Artifact.idempotency_key == idempotency_key,
                    )
                    .first()
                )
                if existing is not None:
                    result = {
                        "id": existing.id,
                        "url": artifact_url(existing.id),
                        "kind": existing.kind.value,
                        "filename": existing.filename,
                        # Same effective-attachment echo as the 201 below (ext#52): a replay must
                        # report where the evidence ACTUALLY sits, not what this retry asked for.
                        "finding_id": existing.finding_id,
                        "finding_id_dropped": finding_id is not None and existing.finding_id != finding_id,
                    }
                    return jsonify(result), 200

        # Never attach to ANOTHER engagement's finding (ext#40 mechanism 3): ``finding_id`` is a
        # caller-supplied id read straight off the request body with no cross-check against
        # ``engagement_id`` -- the upload itself is tenancy-gated above, but the ATTACHMENT TARGET was
        # not, so an authenticated actor holding ANY engagement could bolt evidence onto a finding in
        # someone else's report, where it would render into that client's deliverable
        # (``reporting/context.py`` builds a finding's evidence gallery straight from
        # ``finding.artifacts``, no engagement cross-check there either). Silently dropping the
        # association (rather than 404ing the whole upload) matches the precedent already established
        # for the PAT machine route (``api_pat.py::scribble_upload_artifact``) -- the artifact still
        # lands on the engagement the caller is authorized for, just unattached. ``finding_id_dropped``
        # in the response lets a well-behaved caller notice and fix its own request instead of the
        # mismatch failing silently.
        finding_id_dropped = False
        if finding_id is not None:
            with open_session() as db:
                target = db.get(EngagementFinding, finding_id)
                if target is None or target.engagement_id != engagement_id:
                    finding_id = None
                    finding_id_dropped = True

        content_type = guess_content_type(filename, data)
        if kind_raw:
            try:
                kind = ArtifactKind(kind_raw)
            except ValueError:
                return jsonify(error=f"invalid kind {kind_raw!r}"), 400
        else:
            kind = _infer_kind(content_type)

        if placement_raw:
            try:
                placement = ArtifactPlacement(placement_raw)
            except ValueError:
                return jsonify(error=f"invalid placement {placement_raw!r}"), 400
        else:
            placement = ArtifactPlacement.attached

        storage_path, sha256, byte_size = save_bytes(cfg, engagement_id, filename, data)

        with open_session() as db:
            artifact = Artifact(
                engagement_id=engagement_id,
                finding_id=finding_id,
                kind=kind,
                placement=placement,
                filename=filename,
                content_type=content_type,
                storage_path=storage_path,
                byte_size=byte_size,
                sha256=sha256,
                caption=caption,
                include_in_report=True,
                created_by=current_actor_username(),
                idempotency_key=idempotency_key,
            )
            db.add(artifact)
            db.commit()
            result = {
                "id": artifact.id,
                "url": artifact_url(artifact.id),
                "kind": artifact.kind.value,
                "filename": artifact.filename,
                "finding_id": artifact.finding_id,
                "finding_id_dropped": finding_id_dropped,
            }
        return jsonify(result), 201

    @api_bp.get("/artifacts/<int:artifact_id>/raw")
    def artifact_raw(artifact_id: int):
        cfg = get_config()
        with open_session() as db:
            artifact = db.get(Artifact, artifact_id)
            if artifact is None:
                return jsonify(error="not found"), 404
            storage_path = artifact.storage_path
            filename = artifact.filename
            content_type = artifact.content_type

        try:
            path = resolve_path(cfg, storage_path)
        except ValueError:
            return jsonify(error="invalid storage path"), 400
        if not path.is_file():
            return jsonify(error="file missing on disk"), 404

        # Untrusted-file handling (mirrors Lotek): always a forced attachment download, never inline —
        # evidence artifacts may be attacker-influenced (e.g. a scraped page) and must never render in
        # the app's own origin.
        return send_file(
            path,
            as_attachment=True,
            download_name=filename,
            mimetype=content_type or "application/octet-stream",
        )

    @api_bp.post("/artifacts/<int:artifact_id>")
    def update_artifact(artifact_id: int):
        payload = request.get_json(silent=True) or {}
        with open_session() as db:
            artifact = db.get(Artifact, artifact_id)
            if artifact is None:
                return jsonify(error="not found"), 404
            if "caption" in payload:
                artifact.caption = payload["caption"]
            if "include_in_report" in payload:
                artifact.include_in_report = bool(payload["include_in_report"])
            if payload.get("kind"):
                try:
                    artifact.kind = ArtifactKind(payload["kind"])
                except ValueError:
                    return jsonify(error=f"invalid kind {payload['kind']!r}"), 400
            db.commit()
            result = _artifact_dict(artifact)
        return jsonify(result)

    @api_bp.post("/artifacts/<int:artifact_id>/delete")
    def delete_artifact(artifact_id: int):
        cfg = get_config()
        with open_session() as db:
            artifact = db.get(Artifact, artifact_id)
            if artifact is None:
                return jsonify(error="not found"), 404
            storage_path = artifact.storage_path
            db.delete(artifact)
            db.commit()
        delete_file(cfg, storage_path)
        return jsonify(ok=True)

    @api_bp.get("/engagements/<int:engagement_id>/artifacts")
    def list_engagement_artifacts(engagement_id: int):
        """List every artifact on this engagement -- ext#51's cookie review surface (the machine
        counterpart is ``api_pat.scribble_list_artifacts``), so an operator can see engagement-level
        evidence (``finding_id`` null) before it publishes into the Evidence appendix, not just a
        finding's own gallery (``GET .../findings/<id>/artifacts`` below, which by construction cannot
        show one).

        No inline tenancy call needed: ``engagement_id`` is a ``_DIRECT_KEYS`` view arg, so the
        blueprint-wide gate (``scribble.authz.register_gate``) already 404s a non-member before this
        view runs -- see ``tests/test_scribble_tenancy_gate.py``.
        """
        with open_session() as db:
            rows = (
                db.query(Artifact)
                .filter(Artifact.engagement_id == engagement_id)
                .order_by(Artifact.order_index, Artifact.id)
                .all()
            )
            result = [_artifact_dict(a) for a in rows]
        return jsonify(artifacts=result)

    @api_bp.get("/findings/<int:finding_id>/artifacts")
    def list_finding_artifacts(finding_id: int):
        with open_session() as db:
            rows = (
                db.query(Artifact)
                .filter(Artifact.finding_id == finding_id)
                .order_by(Artifact.order_index)
                .all()
            )
            result = [_artifact_dict(a) for a in rows]
        return jsonify(artifacts=result)

    @api_bp.post("/findings/<int:finding_id>/artifacts/reorder")
    def reorder_artifacts(finding_id: int):
        payload = request.get_json(silent=True) or {}
        order = payload.get("order")
        if not isinstance(order, list):
            return jsonify(error="order must be a list of artifact ids"), 400
        with open_session() as db:
            rows = {a.id: a for a in db.query(Artifact).filter(Artifact.finding_id == finding_id).all()}
            for index, artifact_id in enumerate(order):
                artifact = rows.get(_as_int(artifact_id))
                if artifact is not None:
                    artifact.order_index = index
            db.commit()
        return jsonify(ok=True)
