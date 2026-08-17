"""PAT-scoped MACHINE API for Scribble — mounted at ``<url_prefix>/machine`` on its OWN blueprint.

Lets host TOOLS drive Scribble the host's way (``Authorization: Bearer lotek_pat_…`` + scope RBAC)
rather than through the cookie-authed browser API. Moved out of the host's core in the
reporting-decoupling refactor: the host now owns only scan data, tenancy, and the Bearer scheme, all
reached through ``scribble.host`` (injected ``extras``), never by importing host modules.

SECURITY (four layers, all fail-closed):
  1. ``machine_bp.before_request = scribble.host.authenticate`` — every route on this blueprint needs a
     valid token even if a route forgets the decorator below. 503 when unmounted/no host.
  2. ``@host.require_scope("read"|"write")`` per route — scope + "a write token can't out-rank a
     demoted owner".
  3. Job-level TENANCY is decided by the HOST (``host.findings.get_job``/``get_finding`` apply
     ``user_can_view_job`` internally and return None for both missing and unauthorized). This module
     never sees, and cannot widen, that decision — it just 404s on None.
  4. ENGAGEMENT-level tenancy: ``scribble.authz.can_view_engagement(engagement, host.actor())`` on every
     route that touches one, and ``can_view_client_id`` on the create route's body-supplied ``client_id``.

Layer 4 was MISSING until 2026-08-12, and its absence is worth stating plainly because layer 3 is what
disguised it: ``promote-job`` carefully asked the host whether the caller could read the SOURCE job, then
wrote the result into whatever engagement id the URL named. A ``write``-scoped token could author findings
into any client's report (`add_finding`), bulk-copy its own scan results into a report it cannot read
(`promote-job` — data crossing outward, the worse half), or create an engagement under any client id it
cared to name (`create_engagement`). "The job is authorized" is not "the destination is authorized"; a
route that moves data between two tenancy domains must check BOTH ends.

``authorize_engagement_view`` (the cookie blueprints' aborting wrapper, and the ``before_request`` gate
built on it) can't be reused here: both resolve the principal via ``deps.current_actor()`` — the browser
session user — which is None on a PAT request, so a shared gate would 404 every machine route. The
PREDICATE is shared; only the actor lookup differs, and the host's ``can_view_client`` is duck-typed on
``.id`` so it takes a ``PatActor`` just as happily as a session ``User``.

CSRF: the host exempts this prefix because the manifest declares it as a machine surface
([host] machine_prefix). That exemption is ONLY sound because these routes accept no ambient session
cookie — never add a cookie fallback here, and never widen machine_prefix to cover
``<url_prefix>/api`` (the cookie-authed browser JSON, which must stay CSRF-protected).

Promotion/aggregation (vuln-template resolution, parent/child nesting, fact -> variable mapping) is
NOT this module's concern — it lives in ``scribble/promote.py`` (imported lazily, inside the two view
functions that need it, so this module still imports cleanly even before that file exists).
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import select

from scribble import findings_service, host
from scribble.api_schemas import (
    AddFindingRequest,
    BulkMoveFindingsRequest,
    CreateEngagementRequest,
    CreateGroupRequest,
    CreateTemplateRequest,
    MoveFindingRequest,
    PatchFindingRequest,
    ReorderGroupsRequest,
    UpdateGroupRequest,
    UploadArtifactRequest,
    request_body,
)
from scribble.artifacts_api import _as_int, artifact_url
from scribble.artifacts_storage import delete_file, guess_content_type, save_bytes
from scribble.authz import (
    can_view_client_id,
    can_view_engagement,
    host_is_mounted,
    visible_engagements,
)
from scribble.content import schema
from scribble.deps import get_config, open_session, severity_enum
from scribble.enums import ArtifactKind, ArtifactPlacement, Confidence, FindingStatus, OrderMode
from scribble.models import (
    Artifact,
    Engagement,
    EngagementFinding,
    FindingGroup,
    ScribbleVulnMap,
    VulnerabilityTemplate,
)
from scribble.prosemirror_sanitize import sanitize_content_json
from scribble.reporting.context import build_report_context

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

machine_bp = Blueprint("scribble_machine", __name__)
machine_bp.before_request(host.authenticate)

# Upper bound on a single uploaded evidence artifact (see scribble_upload_artifact) — evidence is
# screenshots/captures/small docs, so 25 MiB is generous while stopping a write token from exhausting
# memory/disk with one giant payload.
_MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


# ── pure helpers (moved verbatim from the deleted src/app/api_v1_scribble.py) ────────────────────────


def _enum_value(value) -> str | None:
    """The ``.value`` of an enum column, or the raw value when it is already a plain string (a mounted
    host may inject its own ``Severity``, and SQLAlchemy hands some columns back as ``str``). None-safe.
    Was ``_sev_value``; generalized when the findings serializers below needed the same unwrap for
    ``confidence``/``status``/``order_mode``/``kind`` rather than four more copies of it."""
    return getattr(value, "value", value) if value is not None else None


def _opt_int(data: dict, key: str):
    """Validate an OPTIONAL integer JSON field. Returns (value_or_None, error_response_or_None) so a
    non-integer value (list/dict/non-numeric string/bool) yields a clean 400 rather than a 500 raised
    deep in db.get (which needs a hashable, coercible id)."""
    v = data.get(key)
    if v is None:
        return None, None
    # bool is an int subclass in Python — reject it explicitly (True/False is not an id).
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None, (jsonify({"error": "bad_request", "detail": f"{key} must be an integer"}), 400)
    try:
        return int(v), None
    except (TypeError, ValueError):
        return None, (jsonify({"error": "bad_request", "detail": f"{key} must be an integer"}), 400)


def _opt_host_id(data: dict, key: str):
    """Validate an OPTIONAL HOST id field (``client_id``) -> (value_or_None, error_response_or_None).

    A host id is whatever the mounted host's PKs are: an ``int`` standalone/legacy, a ``uuid.UUID`` under
    lotek v2 — the two shapes ``models.SoftHostId`` stores. ``_opt_int`` was used here, which meant a v2
    caller could not pass a client at all (``int("0198…")`` -> 400), so every machine-created engagement
    was necessarily client-less — i.e. one nobody could open, since a NULL client denies everyone. Gating
    a field that cannot be set would have been a route that can only fail; hence this.
    """
    v = data.get(key)
    if v is None:
        return None, None
    bad = (jsonify({"error": "bad_request", "detail": f"{key} must be an integer or a UUID"}), 400)
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None, bad
    if isinstance(v, int):
        return v, None
    text = v.strip()
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text), None
    try:
        return uuid.UUID(text), None
    except (ValueError, AttributeError):
        return None, bad


def _engagement_not_found():
    """The ONE refusal a machine caller gets for an engagement it may not touch — byte-identical to the
    one for an engagement that does not exist. 404, never 403: a distinguishable refusal is an existence
    oracle over the whole id space (same posture as ``scribble.authz``'s abort(404))."""
    return jsonify({"error": "not_found", "detail": "engagement not found"}), 404


def _client_not_found():
    """The ONE refusal for a client the caller may not create an engagement under — and the STATIC
    next-step hint that keeps it from being a dead end.

    Reported by a client driving prod over a PAT (ext#47): ``POST /api/v1/clients`` answers 201, the very
    next ``POST /scribble/machine/engagements`` answers 404 ``client not found``, and the caller holds an
    id it just created. Both halves are correct and neither may change:

      * a core client is RECORD-ONLY by design (``api_v1.create_client_api`` sets ``owner_id`` and nothing
        else); the first engagement under it is what mints the membership. ``POST /api/v1/engagements``
        does that, self-granting the creator an ``operator`` membership — and it is ADMIN-ONLY, so a
        non-admin token cannot self-onboard at all and needs someone to grant it. The hint says both,
        because a hint that only named the route would dead-end a non-admin one step later.
      * the refusal stays a 404 and stays IDENTICAL for "no such client" and "exists but you hold no
        grant" — ``host_contract.make_can_view_client`` is membership-only precisely so that ownership
        cannot be an access axis (a cross-tenant escalation: client creation is self-service for any
        write-scoped token and ``upsert_client`` resolves by name globally).

    The hint is therefore STATIC and appended UNCONDITIONALLY. It distinguishes nothing between the two
    cases, so the no-existence-oracle property survives exactly as it was: this function takes no client
    id and reads no row, which is what makes the byte-identity structural rather than a promise
    (``tests/test_machine_findings_crud.py`` pins it).
    """
    return (
        jsonify({
            "error": "not_found",
            "detail": "client not found, or you hold no membership under it. A client created with "
                      "POST /api/v1/clients is record-only: the first engagement under it is what grants "
                      "membership. Create one with POST /api/v1/engagements (admin-only — it self-grants "
                      "the creator an operator membership), or ask an admin to grant you a membership on "
                      "an existing engagement, then retry.",
        }),
        404,
    )


def _opt_str(data: dict, key: str):
    """Validate an OPTIONAL string JSON field -> (stripped_value_or_None, error_response_or_None). A
    non-string (list/dict/number) yields a clean 400 instead of an AttributeError on .strip()."""
    v = data.get(key)
    if v is None:
        return None, None
    if not isinstance(v, str):
        return None, (jsonify({"error": "bad_request", "detail": f"{key} must be a string"}), 400)
    return (v.strip() or None), None


def _match_title(pattern: str, title: str) -> bool:
    """Case-insensitive glob match of a ScribbleVulnMap.title_pattern against a finding's title.
    Operators write e.g. ``*sql injection*`` for a substring match; a plain string with no glob chars
    matches exactly."""
    return fnmatch.fnmatchcase(title.lower(), pattern.lower())


# ── host audit + idempotency seams (INV-AUDIT-03 / app/idempotency.py) ───────────────────────────────
#
# Reached generically through ``host.host_hook`` rather than a dedicated ``host.py`` accessor: this
# module does not own ``scribble/host.py``. Both fail OPEN when unmounted (standalone Scribble has no
# host audit trail and no shared idempotency store) — the same fail-open-when-unmounted posture every
# other tenancy/attribution hook in this package carries.


def _audit(db, verb: str, *, subject_type: str, subject_id=None, before=None, after=None) -> None:
    """Append one ``ext:scribble:<verb>`` audit row through the host seam, in the SAME session/txn as the
    change (``db``), so it commits atomically with it. No-op standalone (no host)."""
    hook = host.host_hook("audit")
    if hook is None:
        return
    hook(
        db,
        f"ext:scribble:{verb}",
        subject_type=subject_type,
        subject_id=subject_id,
        before=before,
        after=after,
    )


def _idempotency_key(data: Any) -> str | None:
    """The retry key for a mutating request: the ``Idempotency-Key`` header, else a body ``idempotency_key``
    field. Empty/absent -> None (idempotency is opt-in per request)."""
    body_key = data.get("idempotency_key") if isinstance(data, dict) else None
    header_key = request.headers.get("Idempotency-Key")
    return (header_key or body_key) or None


def _with_idempotency(
    key: str | None, produce: Callable[[], tuple[dict, int]]
) -> tuple[dict, int]:
    """Run ``produce`` (``() -> (body_dict, status)``) through the host idempotency seam when a key AND a
    host are present, so a retried POST replays the stored response instead of executing twice; otherwise
    run it directly. The seam's DB unique constraint (not Python) arbitrates the concurrent-retry race."""
    hook = host.host_hook("idempotent")
    if hook is None or not key:
        return produce()
    return hook(host.actor(), key, produce)


# ── report rendering helpers (reused by the machine report route) ────────────────────────────────────


def _artifact_bytes_reader(artifact_root: Path) -> Callable[[str], bytes | None]:
    """A ``storage_path -> bytes`` reader confined to ``artifact_root`` — mirrors
    ``report_html_api._make_artifact_bytes`` (path-escape guard + size ceiling)."""
    root = artifact_root.resolve()

    def _read(storage_path: str) -> bytes | None:
        if not storage_path:
            return None
        candidate = (root / storage_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None  # path would escape the artifact root — refuse
        if not candidate.is_file():
            return None
        try:
            if candidate.stat().st_size > _MAX_ARTIFACT_BYTES:
                return None
            return candidate.read_bytes()
        except OSError:
            return None

    return _read


def _inline_url_factory(engagement: Engagement, make_inline_artifact_url) -> Callable[[int], str]:
    """``artifact_url`` for ``build_report_context``: resolves an inline-image node's artifact id to the
    renderer-specific placeholder that bakes in the artifact's storage_path."""
    by_id = {a.id: a.storage_path for a in engagement.artifacts}

    def _url(artifact_id: int) -> str:
        return make_inline_artifact_url(by_id.get(artifact_id))

    return _url


def _author_content_json(data: dict) -> dict:
    """Build a SANITIZED ``{block_name: prosemirror_doc}`` mapping for a directly-authored finding/template.

    A supplied ``content_json`` dict wins per-block; plain-text ``description``/``remediation`` (and, for a
    finding, ``references``) fill any block it does not provide. EVERY block — supplied JSON and
    text-wrapped alike — passes through the ProseMirror sanitizer before it is returned, so no
    write-scoped caller can persist markup that would execute when the report is opened. (Text wrapped by
    ``schema.doc_from_text`` passes the sanitizer through unchanged.)"""
    raw: dict[str, Any] = {}
    supplied = data.get("content_json")
    if isinstance(supplied, dict):
        raw.update(supplied)
    for block in ("description", "remediation"):
        if block not in raw:
            text = data.get(block)
            if isinstance(text, str) and text.strip():
                raw[block] = schema.doc_from_text(text)
    if "references" not in raw:
        refs = data.get("references")
        if isinstance(refs, list):
            refs_text = "\n".join(str(r).strip() for r in refs if str(r).strip())
            if refs_text:
                raw["references"] = schema.doc_from_text(refs_text)
    return sanitize_content_json(raw)


# ── 1. POST /engagements ─────────────────────────────────────────────────────────────────────────────


@machine_bp.post("/engagements")
@host.require_scope("write")
@request_body(CreateEngagementRequest)
def scribble_create_engagement():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "bad_request", "detail": "name is required"}), 400
    client_id, err = _opt_host_id(data, "client_id")
    if err:
        return err
    actor = host.actor()

    # The client an engagement is created under IS its tenancy, and it arrives in the request body — so
    # no id-shaped gate can reach it; it is checked here, before the row exists.
    #
    # Mounted, the client is REQUIRED. The host answers False for a NULL client (see
    # host_contract.make_can_view_client), so a client-less engagement is unreadable and unwritable by
    # everyone, its creator included — creating one is a success response for work that produced nothing
    # usable. Refusing is the honest answer; standalone (no host bundle) keeps the old behaviour, since
    # there it is a perfectly ordinary engagement.
    if host_is_mounted():
        if client_id is None:
            return (
                jsonify({
                    "error": "bad_request",
                    "detail": "client_id is required: a mounted engagement is scoped by its client, and "
                              "one with no client is readable by nobody",
                }),
                400,
            )
        if not can_view_client_id(client_id, actor):
            # 404 on the CLIENT for the same reason as on an engagement: do not confirm which client ids
            # exist to a token holding no grant under them. The detail carries a static next-step hint —
            # see _client_not_found for why appending it unconditionally keeps that property.
            return _client_not_found()

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            eng = Engagement(
                name=name,
                scope_type=(data.get("scope_type") or "external"),
                company_name=(data.get("company_name") or None),
                client_id=client_id,
                created_by=actor.username if actor else None,
                # owner_id is unconditional now: scribble owns Engagement/EngagementFinding outright, so
                # it cannot be older than itself (no more capability-gating on the mounted schema).
                owner_id=actor.id if actor else None,
            )
            db.add(eng)
            db.flush()  # assign the PK so the audit row + response can reference it
            body = {"id": eng.id, "name": eng.name}
            _audit(db, "create_engagement", subject_type="engagement", subject_id=eng.id, after=body)
            db.commit()
            return body, 201

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


# ── 2. GET /templates ────────────────────────────────────────────────────────────────────────────────


@machine_bp.get("/templates")
@host.require_scope("read")
def scribble_list_templates():
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    severity = (request.args.get("severity") or "").strip().lower()
    with open_session() as db:
        stmt = select(VulnerabilityTemplate).where(VulnerabilityTemplate.active.is_(True))
        if q:
            stmt = stmt.where(VulnerabilityTemplate.name.ilike(f"%{q}%"))
        if category:
            stmt = stmt.where(VulnerabilityTemplate.category == category)
        rows = db.execute(stmt.order_by(VulnerabilityTemplate.name)).scalars().all()
        # Severity is an enum column; filter in Python to stay robust to enum storage form.
        if severity:
            rows = [t for t in rows if _enum_value(t.default_severity) == severity]
        items = [
            {
                "id": t.id,
                "name": t.name,
                "category": t.category,
                "default_severity": _enum_value(t.default_severity),
                "cvss_score": t.cvss_score,
            }
            for t in rows
        ]
    return jsonify({"count": len(items), "items": items})


# ── 3. GET /templates/<id> ───────────────────────────────────────────────────────────────────────────


@machine_bp.get("/templates/<int:template_id>")
@host.require_scope("read")
def scribble_get_template(template_id: int):
    with open_session() as db:
        t = db.get(VulnerabilityTemplate, template_id)
        if t is None or not t.active:
            return jsonify({"error": "not_found", "detail": "template not found"}), 404
        return jsonify(
            {
                "id": t.id,
                "name": t.name,
                "category": t.category,
                "default_severity": _enum_value(t.default_severity),
                "cvss_score": t.cvss_score,
                "cvss_vector": t.cvss_vector,
                "references": t.references or [],
                "content_json": t.content_json or {},
            }
        )


# ── 3b. POST /templates — author a reusable vuln template ────────────────────────────────────────────


@machine_bp.post("/templates")
@host.require_scope("write")
@request_body(CreateTemplateRequest)
def scribble_create_template():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "bad_request", "detail": "name is required"}), 400
    category, err = _opt_str(data, "category")
    if err:
        return err
    cvss_vector, err = _opt_str(data, "cvss_vector")
    if err:
        return err

    SeverityEnum = severity_enum()
    sev_raw = (data.get("default_severity") or "medium")
    if not isinstance(sev_raw, str):
        return jsonify({"error": "bad_request", "detail": "default_severity must be a string"}), 400
    try:
        default_severity = SeverityEnum(sev_raw.strip().lower())
    except ValueError:
        return (
            jsonify({
                "error": "bad_request",
                "detail": "default_severity must be one of info|low|medium|high|critical",
            }),
            400,
        )

    references = data.get("references")
    references = [str(r) for r in references] if isinstance(references, list) else []
    # description/remediation are packed into content_json blocks and sanitized (references live in the
    # template's own ``references`` column, so they are NOT folded into a content block here).
    content_json = _author_content_json({
        "description": data.get("description"),
        "remediation": data.get("remediation"),
        "content_json": data.get("content_json"),
    })

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            template = VulnerabilityTemplate(
                # Authored over the machine API: instantiable by id, but NEVER auto-adopted by
                # promotion into another tenant's report. See models.VulnerabilityTemplate.
                machine_authored=True,
                name=name,
                category=category,
                default_severity=default_severity,
                cvss_vector=cvss_vector,
                content_json=content_json,
                references=references,
                active=True,
            )
            db.add(template)
            db.flush()
            body = {"id": template.id}
            _audit(
                db, "create_template", subject_type="vuln_template", subject_id=template.id,
                after={"id": template.id, "name": name, "default_severity": default_severity.value},
            )
            db.commit()
            return body, 201

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


# ── 4. POST /engagements/<id>/findings ───────────────────────────────────────────────────────────────


@machine_bp.post("/engagements/<int:engagement_id>/findings")
@host.require_scope("write")
@request_body(AddFindingRequest)
def scribble_add_finding(engagement_id: int):
    actor = host.actor()
    actor_username = actor.username if actor else None
    with open_session() as db:
        # DESTINATION tenancy FIRST — before the body is even parsed. Authorizing ahead of validation
        # keeps the refusal for a foreign engagement identical no matter what the body says, so a caller
        # can't map the id space by diffing 400s against 404s.
        engagement = db.get(Engagement, engagement_id)
        if engagement is None:
            return _engagement_not_found()
        if not can_view_engagement(engagement, actor):
            return _engagement_not_found()

        data = request.get_json(silent=True) or {}
        template_id, err = _opt_int(data, "template_id")
        if err:
            return err
        # _opt_HOST_id, not _opt_int: this is a CORE finding id, and core v2 keys it on UUIDv7. Parsed as
        # an int here, promoting a scan finding was unreachable on every v2 host (int("0198…") -> 400)
        # — the same failure, for the same reason, that _opt_host_id was written for on client_id.
        lotek_finding_id, err = _opt_host_id(data, "lotek_finding_id")
        if err:
            return err
        group_id, err = _opt_int(data, "group_id")
        if err:
            return err
        # Neither id -> the THIRD branch (author a finding directly from the body); handled AFTER this
        # session closes so its write can run through the idempotency seam. The two id-driven branches
        # return in place below.

        # Resolve the target group; never attach to another engagement's group (defensive).
        group = db.get(FindingGroup, group_id) if group_id else None
        if group is not None and group.engagement_id != engagement_id:
            group = None
        siblings = (
            group.findings if group is not None else [f for f in engagement.findings if f.group_id is None]
        )

        if template_id:
            template = db.get(VulnerabilityTemplate, template_id)
            if template is None or not template.active:  # a retired template isn't instantiable
                return jsonify({"error": "not_found", "detail": "template not found"}), 404
            overrides: dict[str, Any] = {
                "engagement_id": engagement_id,
                "group_id": group.id if group is not None else None,
                "order_index": len(siblings),
                "created_by": actor_username,
            }
            for key in ("target_host", "target_port", "target_url"):
                if data.get(key) is not None:
                    overrides[key] = data[key]
            # The WRITE runs through the idempotency seam (its own session), like the direct-author
            # branch — so a retried template instantiation carrying an Idempotency-Key returns the ORIGINAL
            # finding instead of a duplicate. Unlike the lotek_finding_id branch (which self-dedups on
            # source_finding_id), a template instantiation has NO natural dedup key, so the seam is its
            # only backstop. Only scalars cross into the closure; the template is re-fetched in-session.
            _tmpl_pk = template_id

            def _produce_tmpl() -> tuple[dict, int]:
                with open_session() as wdb:
                    tmpl = wdb.get(VulnerabilityTemplate, _tmpl_pk)
                    if tmpl is None or not tmpl.active:
                        return {"error": "not_found", "detail": "template not found"}, 404
                    finding = EngagementFinding.from_template(tmpl, **overrides)
                    wdb.add(finding)
                    wdb.flush()  # assign the PK for the audit row + response
                    body = {"finding_id": finding.id, "engagement_id": engagement_id}
                    _audit(
                        wdb, "add_finding", subject_type="finding", subject_id=finding.id,
                        after={**body, "template_id": _tmpl_pk},
                    )
                    wdb.commit()
                    return body, 201

            body, status = _with_idempotency(_idempotency_key(data), _produce_tmpl)
            return jsonify(body), status

        if lotek_finding_id:
            # Promote a single lotek scan finding. Tenancy is decided by the HOST — host.findings().
            # get_finding applies user_can_view_job internally and returns None for missing, dangling-job,
            # AND not-authorized alike (fail closed, no existence leak). ``findings()`` itself is None only
            # when unmounted; treated the same as "nothing there" per its own contract.
            findings_ns = host.findings()
            dto = findings_ns.get_finding(lotek_finding_id, actor) if findings_ns is not None else None
            if dto is None:
                return jsonify({"error": "not_found", "detail": "lotek finding not found"}), 404

            # Idempotent promote: if this exact lotek finding was already promoted into this engagement,
            # return the existing authored finding (precise dedup on source_finding_id) rather than
            # creating a duplicate. Done here (not inside promote.promote_one) so a retrying tool never
            # re-runs the heavier template-resolution/content-mapping path for a no-op.
            already = next(
                (f for f in engagement.findings if getattr(f, "source_finding_id", None) == dto.id), None
            )
            if already is not None:
                return (
                    jsonify({"finding_id": already.id, "engagement_id": engagement_id, "deduped": True}),
                    200,
                )

            from scribble.promote import promote_one  # lazy: scribble/promote.py is Track D's file

            finding = promote_one(
                db,
                engagement=engagement,
                group=group,
                dto=dto,
                actor_username=actor_username,
                order_index=len(siblings),
            )
            db.add(finding)  # safe no-op if promote_one already added it to this session
            # Explicit target_host/target_port/target_url in the request body still win over whatever
            # promote_one derived from the dto (matches the pre-refactor route: these were always applied
            # from the request, regardless of the template_id/lotek_finding_id branch).
            for key in ("target_host", "target_port", "target_url"):
                if data.get(key) is not None:
                    setattr(finding, key, data[key])
            db.flush()
            body = {"finding_id": finding.id, "engagement_id": engagement_id}
            _audit(
                db, "add_finding", subject_type="finding", subject_id=finding.id,
                after={**body, "lotek_finding_id": lotek_finding_id},
            )
            db.commit()
            return jsonify(body), 201

        # ── THIRD branch: author a finding directly from the body ──
        # Validation (reads only) runs here, inside the tenancy-checked session; the WRITE runs in a
        # separate produce() so it can be replayed through the idempotency seam. ``title`` + ``severity``
        # become required (there is no template/scan finding to inherit them from). Any supplied
        # content_json is sanitized (allowlisted node/mark set) before persist — a write token cannot
        # store markup that would execute when the report is opened.
        author_title, err = _opt_str(data, "title")
        if err:
            return err
        if not author_title:
            return (
                jsonify({
                    "error": "bad_request",
                    "detail": "template_id, lotek_finding_id, or a title (to author directly) is required",
                }),
                400,
            )
        sev_raw = data.get("severity")
        if not isinstance(sev_raw, str) or not sev_raw.strip():
            return jsonify({
                "error": "bad_request", "detail": "severity is required to author a finding",
            }), 400
        try:
            author_severity = severity_enum()(sev_raw.strip().lower())
        except ValueError:
            return (
                jsonify({
                    "error": "bad_request",
                    "detail": "severity must be one of info|low|medium|high|critical",
                }),
                400,
            )
        author_cvss_vector, err = _opt_str(data, "cvss_vector")
        if err:
            return err
        author_content_json = _author_content_json(data)
        author_group_pk = group.id if group is not None else None
        author_order = len(siblings)
        author_target_host = data.get("target_host")
        author_target_url = data.get("target_url")
        _tp = data.get("target_port")
        author_target_port = str(_tp) if _tp is not None else None

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            finding = EngagementFinding(
                engagement_id=engagement_id,
                group_id=author_group_pk,
                order_index=author_order,
                created_by=actor_username,
                title=author_title,
                severity=author_severity,
                cvss_vector=author_cvss_vector,
                content_json=author_content_json,
                target_host=author_target_host,
                target_port=author_target_port,
                target_url=author_target_url,
            )
            db.add(finding)
            db.flush()
            body = {"finding_id": finding.id, "engagement_id": engagement_id}
            _audit(
                db, "add_finding", subject_type="finding", subject_id=finding.id,
                after={**body, "authored": True, "severity": author_severity.value},
            )
            db.commit()
            return body, 201

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


# ── 5. POST /vuln-map ────────────────────────────────────────────────────────────────────────────────


@machine_bp.post("/vuln-map")
@host.require_scope("write")
def scribble_create_vuln_map():
    data = request.get_json(silent=True) or {}
    template_id, err = _opt_int(data, "template_id")
    if err:
        return err
    if template_id is None:
        return jsonify({"error": "bad_request", "detail": "template_id is required"}), 400
    source, err = _opt_str(data, "source")
    if err:
        return err
    title_pattern, err = _opt_str(data, "title_pattern")
    if err:
        return err
    dedupe_prefix, err = _opt_str(data, "dedupe_prefix")
    if err:
        return err
    if not (source or title_pattern or dedupe_prefix):
        return (
            jsonify({"error": "bad_request", "detail": "at least one match key is required"}),
            400,
        )
    actor = host.actor()
    # The template existence check is a read; do it up front so a missing template 404s WITHOUT claiming
    # an idempotency slot (a validation failure must not be replayed as the stored response for the key).
    with open_session() as db:
        t = db.get(VulnerabilityTemplate, template_id)
        if t is None or not t.active:  # don't map to a missing/retired template
            return jsonify({"error": "not_found", "detail": "template not found"}), 404

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            m = ScribbleVulnMap(
                source=source,
                title_pattern=title_pattern,
                dedupe_prefix=dedupe_prefix,
                template_id=template_id,
                created_by=actor.username if actor else None,
            )
            db.add(m)
            db.flush()
            body = {"id": m.id}
            _audit(
                db, "create_vuln_map", subject_type="vuln_map", subject_id=m.id,
                after={"id": m.id, "template_id": template_id},
            )
            db.commit()
            return body, 201

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


# ── 6. GET /vuln-map ─────────────────────────────────────────────────────────────────────────────────


@machine_bp.get("/vuln-map")
@host.require_scope("read")
def scribble_list_vuln_map():
    with open_session() as db:
        rows = db.execute(select(ScribbleVulnMap).order_by(ScribbleVulnMap.id)).scalars().all()
        items = [
            {
                "id": m.id,
                "source": m.source,
                "title_pattern": m.title_pattern,
                "dedupe_prefix": m.dedupe_prefix,
                "template_id": m.template_id,
            }
            for m in rows
        ]
    return jsonify({"count": len(items), "items": items})


# ── 7. POST /resolve-template ────────────────────────────────────────────────────────────────────────


@machine_bp.post("/resolve-template")
@host.require_scope("read")
def scribble_resolve_template():
    data = request.get_json(silent=True) or {}
    source, err = _opt_str(data, "source")
    if err:
        return err
    title, err = _opt_str(data, "title")
    if err:
        return err
    dedupe_key, err = _opt_str(data, "dedupe_key")
    if err:
        return err

    from scribble.promote import resolve_vuln_template  # lazy: scribble/promote.py is Track D's file

    with open_session() as db:
        template_id = resolve_vuln_template(db, source=source, title=title, dedupe_key=dedupe_key)
        # Re-check the mapped template still exists + is active; a stale mapping resolves to null so
        # the caller cleanly falls back to from_lotek_finding.
        if template_id is not None:
            t = db.get(VulnerabilityTemplate, template_id)
            if t is None or not t.active:
                template_id = None
    return jsonify({"template_id": template_id})


# ── 8. POST /engagements/<id>/promote-job/<job_id> ──────────────────────────────────────────────────


@machine_bp.post("/engagements/<int:engagement_id>/promote-job/<job_id>")
@host.require_scope("write")
def scribble_promote_job(engagement_id: int, job_id: str):
    """Bulk-promote a lotek scan job's Findings into a Scribble engagement.

    This route spans TWO tenancy domains and must check both, which is exactly what it failed to do until
    2026-08-12:
      * the SOURCE job — decided entirely by the HOST (``host.findings().get_job``/``list_findings``
        apply ``user_can_view_job`` internally): unknown job, or one the caller can't view, is 404, no
        existence leak.
      * the DESTINATION engagement — ``can_view_engagement``. Without it a ``write`` token could pour its
        own findings into any client's report: a write into a tenant it holds nothing under, AND its own
        scan data handed to whoever reads that report.

    Aggregation (resolving each finding to a library template, nesting matches under one shared parent,
    everything else bridged verbatim) is ``scribble.promote.promote_job``'s concern, not this module's.
    """
    actor = host.actor()
    actor_username = actor.username if actor else None
    with open_session() as db:
        engagement = db.get(Engagement, engagement_id)
        if engagement is None:
            return _engagement_not_found()
        if not can_view_engagement(engagement, actor):
            return _engagement_not_found()

        findings_ns = host.findings()
        job = findings_ns.get_job(job_id, actor) if findings_ns is not None else None
        if job is None:
            return jsonify({"error": "not_found", "detail": "job not found"}), 404
        dtos = findings_ns.list_findings(job_id, actor) if findings_ns is not None else []

        from scribble.promote import promote_job  # lazy: scribble/promote.py is Track D's file

        result = promote_job(db, engagement=engagement, findings=dtos, actor_username=actor_username)
        db.commit()

    # Record the assignment on the host's own generic Job.promoted_* columns (separate session/engine —
    # this is the ONE write the host contract exposes; it is not part of the scribble-side transaction
    # above). Best-effort: False (missing job / unauthorized / no host) never fails this response, since
    # the promotion itself already succeeded and the job existence/tenancy was already checked above.
    host.mark_job_promoted(job_id, actor, extension="scribble", ref_id=engagement_id)

    return jsonify(
        {
            "engagement_id": engagement_id,
            "promoted": result.get("promoted", 0),
            "skipped": result.get("skipped", 0),
            "parents": result.get("parents", 0),
        }
    )


# ── 8b. GET /engagements — list the engagements this token may see ───────────────────────────────────


def _engagement_summary(engagement: Engagement) -> dict:
    return {
        "id": engagement.id,
        "name": engagement.name,
        "client_id": str(engagement.client_id) if engagement.client_id is not None else None,
        "scope_type": engagement.scope_type,
        "company_name": engagement.company_name,
        "status": engagement.status,
    }


@machine_bp.get("/engagements")
@host.require_scope("read")
def scribble_list_engagements():
    """List engagements the caller may see — SCOPED, never the whole table. ``visible_engagements`` narrows
    to the actor's client set (in SQL when the host exposes the set, else per-row predicate), exactly like
    the cookie dashboard, so a read token never enumerates another tenant's engagements."""
    actor = host.actor()
    with open_session() as db:
        stmt = select(Engagement).order_by(Engagement.id)
        rows = visible_engagements(db, stmt, actor)
        items = [_engagement_summary(e) for e in rows]
    return jsonify({"count": len(items), "items": items})


# ── 8c. GET /engagements/<id> — one engagement the caller may see ────────────────────────────────────


@machine_bp.get("/engagements/<int:engagement_id>")
@host.require_scope("read")
def scribble_get_engagement(engagement_id: int):
    actor = host.actor()
    with open_session() as db:
        engagement = db.get(Engagement, engagement_id)
        # Missing and not-visible are the SAME 404 — no existence oracle over the id space.
        if engagement is None or not can_view_engagement(engagement, actor):
            return _engagement_not_found()
        summary = _engagement_summary(engagement)
        summary["finding_count"] = len(engagement.findings)
        summary["group_count"] = len(engagement.groups)
        summary["artifact_count"] = len(engagement.artifacts)
    return jsonify(summary)


# ── 8d. GET /engagements/<id>/report — stream the rendered deliverable (html|docx) ───────────────────


@machine_bp.get("/engagements/<int:engagement_id>/report")
@host.require_scope("read")
def scribble_engagement_report(engagement_id: int):
    """Stream the fully-rendered report over a PAT — ``?format=html`` (default) or ``?format=docx``. NO
    pdf. Reuses the SAME ``build_report_context`` + renderers the cookie report routes use (artifact bytes
    embedded), so the machine deliverable is byte-identical to the browser one.

    Reading the whole deliverable — every client finding + evidence image — is a DISCLOSURE event, so it
    EMITS an ``ext:scribble:report_read`` audit row (who/what/format) even though it mutates no report
    data. Tenancy is the same ``can_view_engagement`` predicate as every other engagement-scoped route;
    missing and not-visible are the same 404."""
    actor = host.actor()
    fmt = (request.args.get("format") or "html").strip().lower()
    if fmt not in ("html", "docx"):
        return jsonify({"error": "bad_request", "detail": "format must be html or docx"}), 400

    cfg = get_config()
    reader = _artifact_bytes_reader(cfg.artifact_root)
    with open_session() as db:
        engagement = db.get(Engagement, engagement_id)
        if engagement is None or not can_view_engagement(engagement, actor):
            return _engagement_not_found()

        if fmt == "docx":
            from scribble.reporting.render_docx import make_inline_artifact_url, render_report_docx

            ctx = build_report_context(
                engagement, artifact_url=_inline_url_factory(engagement, make_inline_artifact_url)
            )
            payload: bytes | str = render_report_docx(ctx, artifact_bytes=reader)
            mimetype = _DOCX_MIME
        else:
            from scribble.reporting.render_html import make_inline_artifact_url, render_report_html

            ctx = build_report_context(
                engagement, artifact_url=_inline_url_factory(engagement, make_inline_artifact_url)
            )
            payload = render_report_html(ctx, inline_assets=True, artifact_bytes=reader)
            mimetype = "text/html"

        _audit(
            db, "report_read", subject_type="engagement", subject_id=engagement_id,
            after={"format": fmt, "actor": actor.username if actor else None},
        )
        db.commit()  # persist the disclosure audit row

    return Response(payload, mimetype=mimetype)


# ── 9. POST /engagements/<id>/artifacts — evidence/screenshot upload ─────────────────────────────────


@machine_bp.post("/engagements/<int:engagement_id>/artifacts")
@host.require_scope("write")
@request_body(UploadArtifactRequest)
def scribble_upload_artifact(engagement_id: int):
    """Attach an evidence file (screenshot, capture, document) to an engagement — the PAT counterpart of
    the cookie ``POST <url_prefix>/api/artifacts``, so an agent can supply report evidence.

    Accepts either a ``multipart/form-data`` upload (``file`` field) or a JSON body with base64 content
    (``content_base64``/``data_base64``/``data``). The engagement is taken from the URL (not the body),
    and TENANCY is the same predicate the rest of this module uses — ``can_view_engagement(engagement,
    host.actor())`` — checked BEFORE the body is even parsed; missing and not-visible are the same 404
    (no existence oracle). ``idempotency_key`` (body or ``Idempotency-Key`` header) makes a retry return
    the original artifact (200) rather than a duplicate.
    """
    actor = host.actor()

    # DESTINATION tenancy FIRST — before the body is read, exactly as ``scribble_add_finding`` does it and
    # for the reason stated there: authorizing ahead of validation keeps the refusal for a foreign
    # engagement identical no matter what the body says, so a caller cannot map the id space by diffing
    # 400s against 404s. This route used to parse the body first, which also meant an UNAUTHORIZED caller
    # could make the server buffer up to _MAX_ARTIFACT_BYTES of multipart (or decode a base64 blob) before
    # anything checked whether it was allowed to write here at all — work done on behalf of a tenant with
    # no grant. The re-read below is what the write itself uses.
    with open_session() as db:
        engagement = db.get(Engagement, engagement_id)
        if engagement is None or not can_view_engagement(engagement, actor):
            return _engagement_not_found()

    upload = request.files.get("file")
    if upload is not None:
        caption = request.form.get("caption")
        kind_raw = request.form.get("kind")
        placement_raw = request.form.get("placement")
        idempotency_key = request.form.get("idempotency_key")
        fid = _as_int(request.form.get("finding_id"))
        filename = upload.filename or "artifact"
        data = upload.read(_MAX_ARTIFACT_BYTES + 1)  # bound the read; the len() check below rejects >max
    elif request.is_json:
        payload = request.get_json(silent=True) or {}
        caption = payload.get("caption")
        kind_raw = payload.get("kind")
        placement_raw = payload.get("placement")
        idempotency_key = payload.get("idempotency_key")
        fid = _as_int(payload.get("finding_id"))
        filename = payload.get("filename") or "artifact"
        content_b64 = payload.get("content_base64") or payload.get("data_base64") or payload.get("data")
        if not content_b64:
            return jsonify({"error": "bad_request", "detail": "content_base64 is required"}), 400
        if not isinstance(content_b64, str):
            return jsonify({"error": "bad_request", "detail": "content_base64 must be a base64 string"}), 400
        # Preflight size cap: avoid decoding a huge base64 blob into memory only to reject it later.
        max_b64_len = ((_MAX_ARTIFACT_BYTES + 2) // 3) * 4 + 4  # allow padding/newlines
        if len(content_b64) > max_b64_len:
            return jsonify({
                "error": "payload_too_large",
                "detail": f"artifact exceeds the {_MAX_ARTIFACT_BYTES // (1024 * 1024)} MiB limit",
            }), 413
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "bad_request", "detail": "invalid base64 content"}), 400
    else:
        return jsonify({"error": "bad_request", "detail": "expected a multipart file or a JSON body"}), 400

    if not data:
        return jsonify({"error": "bad_request", "detail": "empty upload"}), 400
    # Bound the artifact so one authenticated write token can't exhaust memory/disk with a giant blob.
    # Evidence is screenshots/captures/small docs; 25 MiB is generous. Checked after decode (the base64
    # string is ~1.33x, so an oversized payload is caught here rather than buffered to disk).
    if len(data) > _MAX_ARTIFACT_BYTES:
        return jsonify({
            "error": "payload_too_large",
            "detail": f"artifact exceeds the {_MAX_ARTIFACT_BYTES // (1024 * 1024)} MiB limit",
        }), 413

    # (Tenancy was decided at the TOP of this function, before the body was read — see the comment
    # there. It is deliberately NOT re-checked here: a second identical check would be a second DB
    # round-trip per upload, and worse, it would leave two blocks where a maintainer told to "remove the
    # duplicate" could delete the FIRST one and silently restore the parse-before-authorize ordering
    # this route was fixed to remove.)

    idempotency_key = (idempotency_key or request.headers.get("Idempotency-Key") or None)
    if idempotency_key:
        with open_session() as db:
            existing = (
                db.query(Artifact)
                .filter(Artifact.engagement_id == engagement_id, Artifact.idempotency_key == idempotency_key)
                .first()
            )
            if existing is not None:
                return jsonify({
                    "id": existing.id, "url": artifact_url(existing.id),
                    "kind": existing.kind.value, "filename": existing.filename,
                }), 200

    content_type = guess_content_type(filename, data)
    if kind_raw:
        try:
            kind = ArtifactKind(kind_raw)
        except ValueError:
            return jsonify({"error": "bad_request", "detail": f"invalid kind {kind_raw!r}"}), 400
    else:
        kind = ArtifactKind.screenshot if content_type.startswith("image/") else (
            ArtifactKind.text if content_type.startswith("text/") else ArtifactKind.file
        )
    if placement_raw:
        try:
            placement = ArtifactPlacement(placement_raw)
        except ValueError:
            return jsonify({"error": "bad_request", "detail": f"invalid placement {placement_raw!r}"}), 400
    else:
        placement = ArtifactPlacement.attached

    storage_path, sha256, byte_size = save_bytes(get_config(), engagement_id, filename, data)
    with open_session() as db:
        # Never attach to ANOTHER engagement's finding — the same defensive rule `add_finding` applies to
        # `group_id`. `finding_id` is a caller-supplied id that was written straight through: the upload
        # itself is gated on `engagement_id`, but the ATTACHMENT target was not, and
        # `reporting/context.py` builds a finding's evidence gallery from `finding.artifacts` with no
        # engagement cross-check. So a write token holding any engagement could bolt an attacker-chosen
        # image and caption onto a finding in someone else's report, where it renders into that client's
        # deliverable. Silently dropping the association (rather than 404ing) matches the `group_id`
        # precedent: the artifact still lands on the engagement the URL named, unattached.
        if fid is not None:
            target = db.get(EngagementFinding, fid)
            if target is None or target.engagement_id != engagement_id:
                fid = None
        artifact = Artifact(
            engagement_id=engagement_id,
            finding_id=fid,
            kind=kind,
            placement=placement,
            filename=filename,
            content_type=content_type,
            storage_path=storage_path,
            byte_size=byte_size,
            sha256=sha256,
            caption=caption,
            include_in_report=True,
            created_by=actor.username if actor else None,
            idempotency_key=idempotency_key,
        )
        db.add(artifact)
        db.commit()
        return jsonify({
            "id": artifact.id, "url": artifact_url(artifact.id),
            "kind": artifact.kind.value, "filename": artifact.filename,
        }), 201


# ── 10. findings CRUD + board management (ext#41) ─────────────────────────────────────────────────────
#
# Until this section existed the machine API could CREATE a finding and nothing else — no read-back, no
# edit, no delete, no grouping, no ordering. An agent authoring a deliverable over a PAT therefore could
# not fix a title without deleting and recreating the finding, which it could not do either, and which
# would have thrown away that finding's group membership and its position in the report. Reported by a
# client after authoring a real deliverable (ext#41, punch-list items 9-12).
#
# Three rules hold across every route below, and they are why this is a SURFACE addition rather than a new
# authorization model:
#
#   1. TENANCY IS RESOLVED FROM THE ENGAGEMENT, never from the request body. A route keyed on a child id
#      (``/findings/<fid>``) loads the child, follows it to its engagement, and asks the same
#      ``can_view_engagement`` predicate every other engagement-scoped route in this module asks. So a
#      finding belonging to another engagement is NOT addressable — and because missing and not-visible
#      answer the SAME 404, it is indistinguishable from one that never existed.
#   2. AUTHORIZE BEFORE PARSING, as ``scribble_add_finding``/``scribble_upload_artifact`` already do and
#      for the reason stated there: a refusal that varied with the body (400 vs 404) would itself answer
#      "does this id exist?" for every id probed.
#   3. THE MUTATION LOGIC IS NOT WRITTEN HERE. ``scribble/findings_service.py`` owns the ordering,
#      grouping and cascade algorithms, and the COOKIE board calls the same functions — see that module's
#      docstring. Two copies of "where does this finding land, and what do its neighbours' ``order_index``
#      values become" is how a deliverable ends up rendering in an order nobody chose.
#
# Every mutating route here also emits its ``_audit`` row and honours the ``Idempotency-Key`` seam, like
# its neighbours above.


def _finding_not_found():
    """The ONE refusal for a finding the caller may not touch — byte-identical to the one for a finding
    that does not exist, for the same reason as :func:`_engagement_not_found`. Covers both "no such id"
    and "that id belongs to an engagement outside your grants"."""
    return jsonify({"error": "not_found", "detail": "finding not found"}), 404


def _group_not_found():
    """Missing group, or a group belonging to a DIFFERENT engagement than the one being operated on —
    one refusal for both, so a caller cannot enumerate group ids across engagements by diffing them."""
    return jsonify({"error": "not_found", "detail": "group not found on this engagement"}), 404


def _visible_engagement(db, engagement_id, actor) -> Engagement | None:
    """The engagement, or None for BOTH missing and not-visible (the caller 404s identically)."""
    engagement = db.get(Engagement, engagement_id)
    if engagement is None or not can_view_engagement(engagement, actor):
        return None
    return engagement


def _visible_finding(db, finding_id, actor) -> EngagementFinding | None:
    """The finding, or None when it does not exist OR its ENGAGEMENT is outside the actor's grants.

    This is rule 1 of the section banner: the tenancy anchor is the row's own ``engagement_id``, read from
    the database, never an engagement id the caller supplied alongside it. A caller therefore cannot pair
    one of its own engagement ids with another tenant's finding id.
    """
    finding = db.get(EngagementFinding, finding_id)
    if finding is None:
        return None
    engagement = db.get(Engagement, finding.engagement_id)
    if engagement is None or not can_view_engagement(engagement, actor):
        return None
    return finding


def _group_of(db, engagement_id, group_id) -> FindingGroup | None:
    """A group BY ID, but only if it belongs to ``engagement_id`` — else None. The same defensive rule
    ``scribble_add_finding`` applies to a body-supplied ``group_id``, hoisted so every board route uses
    one implementation of it."""
    group = db.get(FindingGroup, group_id)
    if group is None or group.engagement_id != engagement_id:
        return None
    return group


# ── serializers ──────────────────────────────────────────────────────────────────────────────────────


def _artifact_summary(artifact: Artifact) -> dict:
    return {
        "id": artifact.id,
        "filename": artifact.filename,
        "kind": _enum_value(artifact.kind),
        "placement": _enum_value(artifact.placement),
        "caption": artifact.caption,
        "order_index": artifact.order_index,
        "include_in_report": artifact.include_in_report,
        "url": artifact_url(artifact.id),
    }


def _finding_summary(finding: EngagementFinding) -> dict:
    """The board-shaped view of a finding: enough to LIST, order and address it, without its prose."""
    return {
        "id": finding.id,
        "title": finding.title,
        "severity": _enum_value(finding.severity),
        "confidence": _enum_value(finding.confidence),
        "status": _enum_value(finding.status),
        "category": finding.category,
        "cvss_score": finding.cvss_score,
        "cvss_vector": finding.cvss_vector,
        "group_id": finding.group_id,
        "order_index": finding.order_index,
        "parent_id": finding.parent_id,
        "include_in_report": finding.include_in_report,
        "target_host": finding.target_host,
        "target_port": finding.target_port,
        "target_url": finding.target_url,
    }


def _finding_detail(db, finding: EngagementFinding) -> dict:
    """The full view: prose blocks, evidence, and the promoted per-host CHILDREN.

    Children are included because they are otherwise invisible to a machine caller: nesting is produced by
    promotion (``promote.py``), a child carries its own target and evidence, and there is no ORM
    relationship to walk — only ``parent_id``. An agent that could not see them would keep re-promoting
    or re-authoring rows that already exist.
    """
    detail = _finding_summary(finding)
    children = db.scalars(
        select(EngagementFinding)
        .where(EngagementFinding.parent_id == finding.id)
        .order_by(EngagementFinding.order_index, EngagementFinding.id)
    ).all()
    detail.update({
        "engagement_id": finding.engagement_id,
        "template_id": finding.template_id,
        # SoftHostId: an int on a legacy/standalone host, a uuid.UUID under lotek v2. Stringified for a
        # stable JSON shape whichever host is mounted — the same thing _engagement_summary does.
        "source_finding_id": (
            str(finding.source_finding_id) if finding.source_finding_id is not None else None
        ),
        "analyst_notes": finding.analyst_notes,
        "created_by": finding.created_by,
        "content_json": finding.content_json or {},
        "variables": finding.variables or {},
        "artifacts": [
            _artifact_summary(a) for a in sorted(finding.artifacts, key=lambda a: a.order_index)
        ],
        "children": [_finding_summary(c) for c in children],
    })
    return detail


def _group_summary(group: FindingGroup) -> dict:
    return {
        "id": group.id,
        "name": group.name,
        "order_index": group.order_index,
        "order_mode": _enum_value(group.order_mode),
        "include_in_report": group.include_in_report,
        "assessment_type_id": group.assessment_type_id,
    }


# ── PATCH body parsing ───────────────────────────────────────────────────────────────────────────────

# Sentinel for "the body did not mention this field at all", which a PATCH must distinguish from an
# explicit ``null`` (= clear a nullable column). ``data.get(key)`` alone conflates the two, which would
# make every PATCH silently clear every field the caller left out.
_ABSENT = object()

_PATCH_TEXT_FIELDS = ("category", "cvss_vector", "target_host", "target_url", "analyst_notes")
_PATCH_CONTENT_FIELDS = ("description", "remediation", "references", "content_json")
_PATCH_ALLOWED = (
    {"title", "severity", "confidence", "status", "cvss_score", "target_port",
     "include_in_report", "idempotency_key"}
    | set(_PATCH_TEXT_FIELDS)
    | set(_PATCH_CONTENT_FIELDS)
)


def _bad_request(detail: str):
    return jsonify({"error": "bad_request", "detail": detail}), 400


def _patch_content_blocks(data: dict):
    """Sanitized ``{block_name: prosemirror_doc}`` for whatever content a PATCH body carried -> (blocks,
    error). Empty dict when the body carried none, which means "leave the prose alone".

    Runs through the SAME ``_author_content_json`` the create route uses, so a PATCH cannot become a
    second, laxer path into ``content_json`` — the sanitizer is the stored-XSS gate for every write-scoped
    caller and it must not be reachable around. Types are validated strictly here (rather than relying on
    ``_author_content_json``'s isinstance checks, which silently IGNORE a wrong-typed field) because on an
    edit route a silently-dropped block reports success for prose that was never saved.
    """
    if not (set(_PATCH_CONTENT_FIELDS) & data.keys()):
        return {}, None
    supplied = data.get("content_json", _ABSENT)
    if supplied is not _ABSENT and not isinstance(supplied, dict):
        return {}, _bad_request("content_json must be an object of {block_name: prosemirror_doc}")
    for key in ("description", "remediation"):
        value = data.get(key, _ABSENT)
        if value is not _ABSENT and not isinstance(value, str):
            return {}, _bad_request(f"{key} must be a string")
    refs = data.get("references", _ABSENT)
    if refs is not _ABSENT and not isinstance(refs, list):
        return {}, _bad_request("references must be a list")
    blocks = _author_content_json({k: data[k] for k in _PATCH_CONTENT_FIELDS if k in data})
    return blocks, None


def _parse_finding_patch(data: dict):
    """Validate a PATCH body -> ``(updates, blocks, error_response_or_None)``.

    ``updates`` maps a model attribute to its new value and holds ONLY fields the body actually supplied:
    absent = unchanged, explicit ``null`` = cleared (for a nullable column). ``blocks`` is the sanitized
    content mapping from :func:`_patch_content_blocks`.

    UNKNOWN fields are REFUSED rather than ignored. This module's convention is lenient parsing, and this
    route deliberately breaks it: a typo'd field name is an agent's single most likely mistake, and
    ignoring it would return 200 for an edit that did not happen — the "silent success that did nothing"
    failure. ``group_id``/``order_index`` get a pointed message because they are the two fields a caller
    most plausibly expects to work here; they belong to ``move``, which owns re-ordering semantics.
    """
    unknown = set(data) - _PATCH_ALLOWED
    for moved in ("group_id", "order_index"):
        if moved in unknown:
            return None, None, _bad_request(
                f"{moved} is not a PATCH field — use POST /findings/<finding_id>/move"
            )
    if unknown:
        return None, None, _bad_request(f"unknown field(s): {', '.join(sorted(unknown))}")

    updates: dict[str, Any] = {}

    title = data.get("title", _ABSENT)
    if title is not _ABSENT:
        # Non-empty, unlike the cookie form (which silently ignores a blank title): on a machine surface a
        # silent no-op is worse than a refusal — the caller cannot tell its edit was dropped.
        if not isinstance(title, str) or not title.strip():
            return None, None, _bad_request("title must be a non-empty string")
        updates["title"] = title.strip()

    for key in _PATCH_TEXT_FIELDS:
        value = data.get(key, _ABSENT)
        if value is _ABSENT:
            continue
        if value is None:
            updates[key] = None
            continue
        if not isinstance(value, str):
            return None, None, _bad_request(f"{key} must be a string or null")
        updates[key] = value.strip() or None

    port = data.get("target_port", _ABSENT)
    if port is not _ABSENT:
        if port is None:
            updates["target_port"] = None
        elif isinstance(port, bool) or not isinstance(port, (int, str)):
            return None, None, _bad_request("target_port must be a string, an integer, or null")
        else:
            updates["target_port"] = str(port).strip() or None

    score = data.get("cvss_score", _ABSENT)
    if score is not _ABSENT:
        if score is None:
            updates["cvss_score"] = None
        elif isinstance(score, bool) or not isinstance(score, (int, float)):
            return None, None, _bad_request("cvss_score must be a number or null")
        else:
            updates["cvss_score"] = float(score)

    for key, factory in (
        ("severity", severity_enum),
        ("confidence", lambda: Confidence),
        ("status", lambda: FindingStatus),
    ):
        raw = data.get(key, _ABSENT)
        if raw is _ABSENT:
            continue
        enum_cls = factory()
        allowed = "|".join(_enum_value(member) or "" for member in enum_cls)
        if not isinstance(raw, str):
            return None, None, _bad_request(f"{key} must be a string ({allowed})")
        try:
            updates[key] = enum_cls(raw.strip().lower())
        except ValueError:
            return None, None, _bad_request(f"{key} must be one of {allowed}")

    flag = data.get("include_in_report", _ABSENT)
    if flag is not _ABSENT:
        if not isinstance(flag, bool):
            return None, None, _bad_request("include_in_report must be true or false")
        updates["include_in_report"] = flag

    blocks, err = _patch_content_blocks(data)
    if err is not None:
        return None, None, err
    if not updates and not blocks:
        return None, None, _bad_request("no updatable fields supplied")
    return updates, blocks, None


def _apply_content_blocks(finding: EngagementFinding, blocks: dict) -> None:
    """Merge sanitized prose blocks into ``content_json`` and re-derive the cached ``content_html`` for
    exactly those blocks.

    Re-deriving through the SAME ``render_block`` + artifact-URL guess the cookie autosave route uses
    (``autosave_api``) is deliberate: ``content_html`` is the editor/preview cache, and a PATCH that
    updated only the JSON would leave the browser showing prose the report no longer contains. The report
    itself re-renders from ``content_json`` (``reporting/context.py``), so this is cache hygiene, not
    report correctness.

    Reassigns both dicts rather than mutating in place — they are plain JSON columns, not ``MutableDict``,
    so SQLAlchemy only notices a NEW object being set on the attribute.
    """
    from scribble.autosave_api import _artifact_url as _editor_artifact_url
    from scribble.content.render_html import render_block

    content_json = dict(finding.content_json or {})
    content_json.update(blocks)
    finding.content_json = content_json

    content_html = dict(finding.content_html or {})
    for name, doc in blocks.items():
        content_html[name] = render_block(doc, artifact_url=_editor_artifact_url)
    finding.content_html = content_html


# ── 10a. GET /engagements/<id>/findings — the board, read back ───────────────────────────────────────


@machine_bp.get("/engagements/<int:engagement_id>/findings")
@host.require_scope("read")
def scribble_list_findings(engagement_id: int):
    """Every finding in an engagement, IN BOARD ORDER, grouped exactly as the report will render it.

    ``GET /engagements/<id>`` answers a bare ``finding_count``, so before this route a machine caller
    could not read back what it had created, let alone address it by id. Group order is
    ``FindingGroup.order_index``; within a group, ``findings_service.display_order`` applies the group's
    own ``order_mode`` — the same ordering ``reporting/context.py`` reads, so this listing IS the document
    order rather than an approximation of it.
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        groups = sorted(engagement.groups, key=lambda g: g.order_index)
        body = {
            "engagement_id": engagement_id,
            "count": len(engagement.findings),
            "groups": [
                {
                    **_group_summary(group),
                    "findings": [
                        _finding_summary(f)
                        for f in findings_service.display_order(group.findings, group.order_mode)
                    ],
                }
                for group in groups
            ],
            "ungrouped": [
                _finding_summary(f) for f in findings_service.ungrouped_display_order(engagement)
            ],
        }
    return jsonify(body)


# ── 10b. GET /findings/<id> — one finding, in full ───────────────────────────────────────────────────


@machine_bp.get("/findings/<int:finding_id>")
@host.require_scope("read")
def scribble_get_finding(finding_id: int):
    actor = host.actor()
    with open_session() as db:
        finding = _visible_finding(db, finding_id, actor)
        if finding is None:
            return _finding_not_found()
        return jsonify(_finding_detail(db, finding))


# ── 10c. PATCH /findings/<id> — edit in place ────────────────────────────────────────────────────────


@machine_bp.patch("/findings/<int:finding_id>")
@host.require_scope("write")
@request_body(PatchFindingRequest)
def scribble_update_finding(finding_id: int):
    """Partially update a finding — the machine counterpart of the cookie finding-detail form plus the
    per-block autosave route, in one call.

    Only the fields the body supplies change; an explicit ``null`` clears a nullable column. Prose arrives
    either as plain text (``description``/``remediation``/``references``) or as ProseMirror
    ``content_json``, and either way it goes through the SAME sanitizer the create route uses.

    Fixing wording was the client's actual complaint: without this, the only recovery was delete and
    recreate — which the machine API could not do either, and which would have lost the finding's group
    and its position.
    """
    actor = host.actor()

    # Tenancy FIRST, before the body is parsed (rule 2 of the section banner).
    with open_session() as db:
        finding = _visible_finding(db, finding_id, actor)
        if finding is None:
            return _finding_not_found()
        authorized_engagement_id = finding.engagement_id

    data = request.get_json(silent=True) or {}
    updates, blocks, err = _parse_finding_patch(data)
    if err is not None:
        return err

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            finding = db.get(EngagementFinding, finding_id)
            # Re-fetched in the write session, so a row deleted between the check above and here is a
            # clean 404 rather than an AttributeError. The engagement_id equality is the load-bearing
            # half: a finding cannot legally change engagement, so if this id moved, the row is not the
            # one that was authorized and the write must not land.
            if finding is None or finding.engagement_id != authorized_engagement_id:
                return {"error": "not_found", "detail": "finding not found"}, 404
            before = {field: _enum_value(getattr(finding, field)) for field in updates}
            for field, value in updates.items():
                setattr(finding, field, value)
            if blocks:
                _apply_content_blocks(finding, blocks)
            after = {field: _enum_value(getattr(finding, field)) for field in updates}
            _audit(
                db, "update_finding", subject_type="finding", subject_id=finding.id,
                before=before,
                # The block NAMES, never their prose: an audit row is not the place to duplicate a
                # client's write-up, and the content itself is already versioned by the row it lands on.
                after={**after, "content_blocks": sorted(blocks)} if blocks else after,
            )
            body = _finding_detail(db, finding)
            db.commit()
            return body, 200

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


# ── 10d. DELETE /findings/<id> ───────────────────────────────────────────────────────────────────────


@machine_bp.delete("/findings/<int:finding_id>")
@host.require_scope("write")
def scribble_delete_finding(finding_id: int):
    """Delete a finding and its evidence — ``findings_service.delete_finding``, the same cascade the
    cookie board performs (artifact ROWS go with it; a group delete only detaches, a finding delete does
    not, because a finding IS its content).

    The on-disk artifact files are unlinked only AFTER the transaction commits, so a rolled-back delete
    cannot leave the bytes gone. On an idempotent REPLAY nothing is unlinked, because ``_produce`` never
    runs — the files went with the original request.
    """
    actor = host.actor()
    cfg = get_config()

    with open_session() as db:
        finding = _visible_finding(db, finding_id, actor)
        if finding is None:
            return _finding_not_found()
        authorized_engagement_id = finding.engagement_id

    removed_paths: list[str] = []

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            finding = db.get(EngagementFinding, finding_id)
            if finding is None or finding.engagement_id != authorized_engagement_id:
                return {"error": "not_found", "detail": "finding not found"}, 404
            before = _finding_summary(finding)
            removed_paths.extend(findings_service.delete_finding(db, finding))
            _audit(
                db, "delete_finding", subject_type="finding", subject_id=finding_id,
                before=before, after=None,
            )
            db.commit()
            return {"deleted": True, "finding_id": finding_id,
                    "engagement_id": authorized_engagement_id}, 200

    body, status = _with_idempotency(_idempotency_key(request.get_json(silent=True) or {}), _produce)
    if status == 200:
        for storage_path in removed_paths:
            delete_file(cfg, storage_path)
    return jsonify(body), status


# ── 10e. POST /findings/<id>/move — one finding into a group / position ──────────────────────────────


def _parse_move_target(data: dict, engagement_id: int, db):
    """Shared parse for both move routes -> ``(target_group, order_index, error_response_or_None)``.

    ``group_id`` must be PRESENT (``null`` = the ungrouped bucket), mirroring the cookie route: a move
    that silently defaulted the destination would be a guess about where the caller wanted the finding.

    A group id that does not exist, or belongs to a DIFFERENT engagement, is a 404 — deliberately NOT the
    silent-drop treatment ``add_finding`` gives a foreign ``group_id``. On a move the destination group IS
    the request: dropping it would move the finding OUT of whatever group it was in, which is data loss
    the caller never asked for, reported as success. The refusal is one message for both cases, so it
    still confirms nothing about which group ids exist.
    """
    if "group_id" not in data:
        return None, 0, _bad_request("group_id is required (use null to move to ungrouped)")
    order_index, err = _opt_int(data, "order_index")
    if err:
        return None, 0, err
    order_index = 0 if order_index is None else order_index

    if data.get("group_id") is None:
        return None, order_index, None
    group_id, err = _opt_int(data, "group_id")
    if err:
        return None, 0, err
    target_group = _group_of(db, engagement_id, group_id)
    if target_group is None:
        return None, 0, _group_not_found()
    return target_group, order_index, None


@machine_bp.post("/findings/<int:finding_id>/move")
@host.require_scope("write")
@request_body(MoveFindingRequest)
def scribble_move_finding(finding_id: int):
    """Set a finding's group and its position — ``{"group_id": <id|null>, "order_index": <int>}``.

    ``order_index`` is a slot in the RENDERED order, exactly as the browser board sends it, and the
    destination group flips to manual ordering. Both are ``findings_service.place_finding``'s semantics,
    shared with the cookie route rather than reimplemented.
    """
    actor = host.actor()
    with open_session() as db:
        finding = _visible_finding(db, finding_id, actor)
        if finding is None:
            return _finding_not_found()
        engagement_id = finding.engagement_id

        data = request.get_json(silent=True) or {}
        target_group, order_index, err = _parse_move_target(data, engagement_id, db)
        if err is not None:
            return err
        target_group_id = target_group.id if target_group is not None else None

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            finding = db.get(EngagementFinding, finding_id)
            if finding is None or finding.engagement_id != engagement_id:
                return {"error": "not_found", "detail": "finding not found"}, 404
            target_group = (
                _group_of(db, engagement_id, target_group_id) if target_group_id is not None else None
            )
            if target_group_id is not None and target_group is None:
                return {"error": "not_found", "detail": "group not found on this engagement"}, 404
            before = {"group_id": finding.group_id, "order_index": finding.order_index}
            findings_service.place_finding(finding, target_group, order_index)
            after = {"group_id": finding.group_id, "order_index": finding.order_index}
            _audit(
                db, "move_finding", subject_type="finding", subject_id=finding_id,
                before=before, after=after,
            )
            body = {
                "finding_id": finding.id,
                "engagement_id": engagement_id,
                "group_id": finding.group_id,
                "order_index": finding.order_index,
            }
            db.commit()
            return body, 200

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


# ── 10f. POST /engagements/<id>/findings/move — BULK ─────────────────────────────────────────────────


@machine_bp.post("/engagements/<int:engagement_id>/findings/move")
@host.require_scope("write")
@request_body(BulkMoveFindingsRequest)
def scribble_move_findings(engagement_id: int):
    """Move SEVERAL findings into one group in a single call —
    ``{"finding_ids": [...], "group_id": <id|null>, "order_index": <int>}``.

    This is the client's "multi-select and drag several findings into a group at once": one call per
    finding works, but it is N round trips and a partial failure leaves the board half-arranged. The
    listed order is preserved (each finding lands at ``order_index + its position in the list``).

    ATOMIC by construction: every id must belong to THIS engagement, and if any does not, the whole
    request is refused with the same ``finding not found`` a nonexistent id gets and NOTHING moves. A
    partial success that silently skipped ids would be indistinguishable from a complete one — and
    skipping is what would make a foreign id a probe rather than a refusal. Duplicate ids are collapsed
    to their first occurrence (a client-side artefact of multi-select, not an error).
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()

        data = request.get_json(silent=True) or {}
        raw_ids = data.get("finding_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return _bad_request("finding_ids must be a non-empty list of finding ids")
        finding_ids: list[int] = []
        for raw in raw_ids:
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                return _bad_request("finding_ids must contain integers")
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                return _bad_request("finding_ids must contain integers")
            if parsed not in finding_ids:
                finding_ids.append(parsed)

        target_group, order_index, err = _parse_move_target(data, engagement_id, db)
        if err is not None:
            return err
        target_group_id = target_group.id if target_group is not None else None

        # Membership of the URL's engagement is checked for EVERY id before anything moves. Note this
        # cannot leak: the engagement itself was already authorized above, so the only thing an id
        # confirms is whether it is in a report the caller can already read in full.
        for fid in finding_ids:
            candidate = db.get(EngagementFinding, fid)
            if candidate is None or candidate.engagement_id != engagement_id:
                return _finding_not_found()

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            target_group = (
                _group_of(db, engagement_id, target_group_id) if target_group_id is not None else None
            )
            if target_group_id is not None and target_group is None:
                return {"error": "not_found", "detail": "group not found on this engagement"}, 404
            moved = []
            for offset, fid in enumerate(finding_ids):
                finding = db.get(EngagementFinding, fid)
                if finding is None or finding.engagement_id != engagement_id:
                    return {"error": "not_found", "detail": "finding not found"}, 404
                findings_service.place_finding(finding, target_group, order_index + offset)
                moved.append({"finding_id": finding.id, "order_index": finding.order_index})
            _audit(
                db, "move_findings", subject_type="engagement", subject_id=engagement_id,
                after={"group_id": target_group_id, "finding_ids": finding_ids},
            )
            body = {"engagement_id": engagement_id, "group_id": target_group_id, "moved": moved}
            db.commit()
            return body, 200

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


# ── 10g. groups: create / update / delete / reorder ──────────────────────────────────────────────────


@machine_bp.post("/engagements/<int:engagement_id>/groups")
@host.require_scope("write")
@request_body(CreateGroupRequest)
def scribble_create_group(engagement_id: int):
    """Create a report section (``FindingGroup``) at the end of the board.

    ``assessment_type_id`` is optional and links the section to a library ``AssessmentType`` (a
    library-wide, tenant-free table). An id that does not resolve is left unset rather than refused —
    the cookie route's behaviour, kept identical here.
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()

    data = request.get_json(silent=True) or {}
    name, err = _opt_str(data, "name")
    if err:
        return err
    if not name:
        return _bad_request("name is required")
    assessment_type_id, err = _opt_int(data, "assessment_type_id")
    if err:
        return err

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                return {"error": "not_found", "detail": "engagement not found"}, 404
            from scribble.models import AssessmentType  # local: keeps the module import list minimal

            assessment_type = (
                db.get(AssessmentType, assessment_type_id) if assessment_type_id is not None else None
            )
            group = findings_service.create_group(
                db, engagement, name=name, assessment_type=assessment_type
            )
            body = _group_summary(group)
            _audit(
                db, "create_group", subject_type="finding_group", subject_id=group.id,
                after={**body, "engagement_id": engagement_id},
            )
            db.commit()
            return body, 201

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


@machine_bp.patch("/engagements/<int:engagement_id>/groups/<int:group_id>")
@host.require_scope("write")
@request_body(UpdateGroupRequest)
def scribble_update_group(engagement_id: int, group_id: int):
    """Rename a section, toggle whether it renders, or set its ordering mode.

    ``order_mode: "auto_severity"`` is the documented way BACK from the manual ordering any move flips a
    group into ("re-rank by severity").
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        if _group_of(db, engagement_id, group_id) is None:
            return _group_not_found()

    data = request.get_json(silent=True) or {}
    unknown = set(data) - {"name", "order_mode", "include_in_report", "idempotency_key"}
    if unknown:
        return _bad_request(f"unknown field(s): {', '.join(sorted(unknown))}")
    updates: dict[str, Any] = {}
    if "name" in data:
        name, err = _opt_str(data, "name")
        if err:
            return err
        if not name:
            return _bad_request("name cannot be empty")
        updates["name"] = name
    if "order_mode" in data:
        raw = data.get("order_mode")
        allowed = "|".join(m.value for m in OrderMode)
        if not isinstance(raw, str):
            return _bad_request(f"order_mode must be one of {allowed}")
        try:
            updates["order_mode"] = OrderMode(raw.strip().lower())
        except ValueError:
            return _bad_request(f"order_mode must be one of {allowed}")
    if "include_in_report" in data:
        if not isinstance(data["include_in_report"], bool):
            return _bad_request("include_in_report must be true or false")
        updates["include_in_report"] = data["include_in_report"]
    if not updates:
        return _bad_request("no updatable fields supplied")

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            group = _group_of(db, engagement_id, group_id)
            if group is None:
                return {"error": "not_found", "detail": "group not found on this engagement"}, 404
            before = {field: _enum_value(getattr(group, field)) for field in updates}
            for field, value in updates.items():
                setattr(group, field, value)
            body = _group_summary(group)
            _audit(
                db, "update_group", subject_type="finding_group", subject_id=group.id,
                before=before, after={field: body[field] for field in updates},
            )
            db.commit()
            return body, 200

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


@machine_bp.delete("/engagements/<int:engagement_id>/groups/<int:group_id>")
@host.require_scope("write")
def scribble_delete_group(engagement_id: int, group_id: int):
    """Delete a report section. Its findings are DETACHED (``group_id`` -> NULL), not deleted — removing a
    section must never silently destroy authored findings. See ``findings_service.delete_group``."""
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        if _group_of(db, engagement_id, group_id) is None:
            return _group_not_found()

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            group = _group_of(db, engagement_id, group_id)
            if group is None:
                return {"error": "not_found", "detail": "group not found on this engagement"}, 404
            before = _group_summary(group)
            detached = [f.id for f in group.findings]
            findings_service.delete_group(db, group)
            _audit(
                db, "delete_group", subject_type="finding_group", subject_id=group_id,
                before={**before, "detached_finding_ids": detached}, after=None,
            )
            db.commit()
            return {"deleted": True, "group_id": group_id, "engagement_id": engagement_id,
                    "detached_finding_ids": detached}, 200

    body, status = _with_idempotency(_idempotency_key(request.get_json(silent=True) or {}), _produce)
    return jsonify(body), status


@machine_bp.post("/engagements/<int:engagement_id>/groups/reorder")
@host.require_scope("write")
@request_body(ReorderGroupsRequest)
def scribble_reorder_groups(engagement_id: int):
    """Set the section order for an engagement — ``{"order": [group_id, ...]}``.

    Defensive, exactly as the cookie route is: a stale, foreign or duplicated id is ignored, and any
    section the payload does not mention keeps its relative order at the end, so a partial payload never
    drops a section or leaves it unpositioned (``findings_service.reorder_groups``).
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()

    data = request.get_json(silent=True) or {}
    order = data.get("order")
    if not isinstance(order, list):
        return _bad_request("order must be a list of group ids")

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            engagement = db.get(Engagement, engagement_id)
            if engagement is None:
                return {"error": "not_found", "detail": "engagement not found"}, 404
            ordered_ids = findings_service.reorder_groups(engagement, order)
            result = [{"id": gid, "order_index": index} for index, gid in enumerate(ordered_ids)]
            _audit(
                db, "reorder_groups", subject_type="engagement", subject_id=engagement_id,
                after={"order": ordered_ids},
            )
            db.commit()
            return {"engagement_id": engagement_id, "order": result}, 200

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


# ── wiring hook ──────────────────────────────────────────────────────────────────────────────────────


def register(machine_bp_: Blueprint) -> None:  # noqa: ARG001 - routes are already bound above at import
    """No-op: unlike the other feature modules (which receive a shared ``api_bp``/``bp`` created
    elsewhere and attach routes to it lazily, guarded against double-registration), this module owns
    ``machine_bp`` itself and binds its routes to it via decorators at import time — the same pattern
    ``scribble/api.py`` uses for its own module-level ``api_bp``. Kept as a callable purely so
    ``scribble/__init__.py::_wire_feature_routes`` can invoke every feature hook uniformly.
    """
