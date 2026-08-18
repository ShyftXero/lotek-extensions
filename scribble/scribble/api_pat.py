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
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request
from sqlalchemy import select

from scribble import host
from scribble.api_schemas import (
    AddFindingRequest,
    CreateEngagementRequest,
    CreateTemplateRequest,
    UpdateArtifactRequest,
    UploadArtifactRequest,
    request_body,
)
from scribble.artifacts_api import artifact_url
from scribble.artifacts_storage import guess_content_type, save_bytes
from scribble.authz import (
    can_view_client_id,
    can_view_engagement,
    host_is_mounted,
    visible_engagements,
)
from scribble.content import schema
from scribble.deps import get_config, open_session, severity_enum
from scribble.enums import ArtifactKind, ArtifactPlacement
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

# A whole ASCII decimal integer and nothing else -- no sign, no exponent, no decimal point. See
# ``_finding_id_or_400`` for why a plain ``int()`` is not good enough here.
#
# ``[0-9]`` rather than ``\d`` ON PURPOSE: ``re``'s ``\d`` is Unicode-aware, so it matched "\u0667"
# (Arabic-Indic seven) and "\uff17" (fullwidth seven), which ``int()`` then coerced to 7 -- an artifact
# attached to, or dropped from, a finding the caller wrote in another script. That is the same coercion
# 2.9 -> 2 was refused for, in a parse whose whole claim is that it validates rather than converts.
_INT_RE = re.compile(r"[0-9]+")

# URL converter for every id in this module's routes. Werkzeug's bare integer converter is UNBOUNDED --
# ``regex=r"\d+"``, ``num_convert=int``, no max -- so ``/engagements/<30 digits>`` routed successfully and
# then 500'd inside ``db.get()`` (measured: OverflowError on SQLite, DataError on Postgres, which also
# poisons the open transaction). Same defect as an out-of-range ``finding_id`` in the BODY, reached through
# the path instead, and a machine API is exactly where a caller-controlled id arrives. Bounding it in the
# CONVERTER means an out-of-range id never reaches a view: Werkzeug does not match the rule and answers
# 404, which is also the right answer for "no such id". The bound is the ``Integer`` PK column's, same as
# ``_MAX_FINDING_ID``. ``tests/test_scribble_machine_tenancy.py`` fails if a machine route is added with an
# unbounded one.
_ID = "int(min=1, max=2147483647)"


# ── pure helpers (moved verbatim from the deleted src/app/api_v1_scribble.py) ────────────────────────


def _sev_value(sev) -> str | None:
    return getattr(sev, "value", sev) if sev is not None else None


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
            # exist to a token holding no grant under them.
            return jsonify({"error": "not_found", "detail": "client not found"}), 404

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
            rows = [t for t in rows if _sev_value(t.default_severity) == severity]
        items = [
            {
                "id": t.id,
                "name": t.name,
                "category": t.category,
                "default_severity": _sev_value(t.default_severity),
                "cvss_score": t.cvss_score,
            }
            for t in rows
        ]
    return jsonify({"count": len(items), "items": items})


# ── 3. GET /templates/<id> ───────────────────────────────────────────────────────────────────────────


@machine_bp.get(f"/templates/<{_ID}:template_id>")
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
                "default_severity": _sev_value(t.default_severity),
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


@machine_bp.post(f"/engagements/<{_ID}:engagement_id>/findings")
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


@machine_bp.post(f"/engagements/<{_ID}:engagement_id>/promote-job/<job_id>")
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


@machine_bp.get(f"/engagements/<{_ID}:engagement_id>")
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


@machine_bp.get(f"/engagements/<{_ID}:engagement_id>/report")
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


# A scribble finding id is a sequential 32-bit ``Integer`` PK. The bound is the COLUMN's, not a policy:
# handing SQLAlchemy a wider integer raises inside the session rather than answering JSON -- OverflowError
# ("Python int too large to convert to SQLite INTEGER") on SQLite, and on Postgres (the real backend) a
# DataError "out of range for type integer" that also poisons the open transaction.
_MAX_FINDING_ID = 2**31 - 1


def _finding_id_or_400(raw) -> tuple[int | None, tuple[Response, int] | None]:
    """``(finding_id, refusal)`` for a caller-supplied ``finding_id``. Exactly one is non-None.

    Absent or empty means "engagement-level evidence" — a legitimate request (the appendix renders it),
    and the multipart surface submits ``finding_id=""`` for an untouched field, so an empty string must
    not be an error.

    Anything else that is not a WHOLE NUMBER IN RANGE is an error, and used to be swallowed: ``_as_int``
    returned None, the artifact silently landed as engagement-level evidence, and the 201 reported
    ``finding_id_dropped: false`` — i.e. "you did not ask for one" — about a request that plainly did. A
    UUID is the specific value to expect (scribble's finding ids are sequential ints while the host's core
    ids are UUIDv7, and confusing the two has taken production down here before), and a runbook reading
    that response cannot tell the difference between a deliberate engagement-level attach and its own bug.

    The parse is deliberately NOT ``int()``, which is why this does not use ``_as_int``. ``int()`` coerces
    rather than validates, and JSON gives a caller two shapes that survive it while meaning something else
    (adversarial review, 2026-08-17):

    * ``2.9`` -> ``2``, so the evidence attaches to finding 2 — an id the caller never named — and the 201
      answers ``finding_id_dropped: false``, asserting the attach was honored exactly as asked.
    * ``true`` -> ``1``, bolting the screenshot onto finding #1, because ``bool`` is an ``int`` subclass.

    Both are gibberish and neither could be refused by a check that asks "does ``int()`` succeed"; the
    docs promise gibberish is refused, so the parse has to be the strict one. Out-of-range is refused for
    a different reason — see ``_MAX_FINDING_ID`` — and refusing it HERE matters because this runs before
    the upload's bytes are written, so a bad id no longer leaves an orphan file on disk behind a 500.

    Refusing all of it is also what keeps ``finding_id_dropped`` meaningful for the case that stays
    silent: a WELL-FORMED id belonging to another engagement is still dropped to None rather than 404'd
    (see the tenancy comment at the write), because answering differently would say whether that id
    exists. A malformed id cannot leak anything, so there is no reason to be quiet about it.
    """
    def refuse() -> tuple[int | None, tuple[Response, int] | None]:
        # Built lazily (``jsonify`` needs an app context) so the accept path stays callable as a plain
        # function -- which is how its parse table is tested directly.
        return None, (jsonify({"error": "bad_request", "detail": "invalid finding_id"}), 400)

    if raw is None or raw == "":
        return None, None
    # ``bool`` first: it IS an ``int``, so the isinstance check below would accept True/False.
    if isinstance(raw, bool):
        return refuse()
    if isinstance(raw, int):
        fid = raw
    elif isinstance(raw, str) and _INT_RE.fullmatch(raw.strip()):
        fid = int(raw.strip())
    else:
        # A float (2.9), a list, a dict, a UUID, "12.5", "1e3" — anything that is not a whole number.
        return refuse()
    if not 0 < fid <= _MAX_FINDING_ID:
        return refuse()
    return fid, None


_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


def _include_in_report_or_400(raw) -> tuple[bool | None, tuple[Response, int] | None]:
    """``(include_in_report, refusal)`` for the caller-supplied publish flag. None means "not specified".

    Strict for the same reason ``_finding_id_or_400`` is: this flag decides whether an artifact appears in
    a CLIENT deliverable, so ``bool(raw)`` -- under which the string ``"false"`` is True -- is the wrong
    parse. JSON callers send a real boolean; the multipart surface can only send text, so the usual word
    forms are accepted there and anything else is refused rather than guessed at.
    """
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True, None
        if word in _FALSE_WORDS:
            return False, None
    return None, (jsonify({"error": "bad_request", "detail": "invalid include_in_report"}), 400)


@machine_bp.post(f"/engagements/<{_ID}:engagement_id>/artifacts")
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
        fid, bad_fid = _finding_id_or_400(request.form.get("finding_id"))
        if bad_fid is not None:
            return bad_fid
        publish, bad_publish = _include_in_report_or_400(request.form.get("include_in_report"))
        if bad_publish is not None:
            return bad_publish
        filename = upload.filename or "artifact"
        data = upload.read(_MAX_ARTIFACT_BYTES + 1)  # bound the read; the len() check below rejects >max
    elif request.is_json:
        payload = request.get_json(silent=True) or {}
        caption = payload.get("caption")
        kind_raw = payload.get("kind")
        placement_raw = payload.get("placement")
        idempotency_key = payload.get("idempotency_key")
        fid, bad_fid = _finding_id_or_400(payload.get("finding_id"))
        if bad_fid is not None:
            return bad_fid
        publish, bad_publish = _include_in_report_or_400(payload.get("include_in_report"))
        if bad_publish is not None:
            return bad_publish
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
                    # Same effective-attachment echo as the 201 below: a replay must report where the
                    # evidence ACTUALLY sits, and a retry whose finding_id differs from the stored one
                    # (e.g. the first attempt's id was dropped as foreign) is told so rather than
                    # reading a bare 200 as "attached, as asked".
                    "finding_id": existing.finding_id,
                    "finding_id_dropped": fid is not None and existing.finding_id != fid,
                    # The STORED decision, not what this retry asked for -- same rule as finding_id above.
                    "include_in_report": existing.include_in_report,
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
        requested_fid = fid
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
            # Whether this evidence SHIPS. Defaults True (the report has always published a finding's
            # attached evidence, and since ext#40 it publishes engagement-level evidence too), but it is
            # now the caller's to decide and the response says which way it went -- so working material can
            # be attached without it turning up in a client deliverable. See
            # ``_include_in_report_or_400`` and ``scribble_update_artifact`` (flip it afterwards).
            include_in_report=publish if publish is not None else True,
            created_by=actor.username if actor else None,
            idempotency_key=idempotency_key,
        )
        db.add(artifact)
        db.commit()
        return jsonify({
            "id": artifact.id, "url": artifact_url(artifact.id),
            "kind": artifact.kind.value, "filename": artifact.filename,
            # Echo the EFFECTIVE attachment, so a caller can tell an attach from a silent drop. The
            # tenancy rule above deliberately does not 404 on a foreign `finding_id` (see its comment),
            # which used to make the two cases indistinguishable at the wire: both answered 201 with a
            # URL, and a dropped one then landed as engagement-level evidence. `finding_id` is what the
            # artifact is actually attached to (null = the engagement itself) and `finding_id_dropped`
            # says the request asked for one that was not honored. ``requested_fid`` is the PARSED id and
            # is trustworthy because an unparseable one never reaches here (``_finding_id_or_400``): if it
            # is None the caller really did ask for engagement-level evidence, so a false
            # ``finding_id_dropped: false`` is not reachable through a malformed value.
            "finding_id": artifact.finding_id,
            "finding_id_dropped": requested_fid is not None and artifact.finding_id != requested_fid,
            # Whether this artifact will appear in the rendered report. Echoed for the same reason as the
            # two fields above: an engagement-level upload is PUBLISHED by default (the Evidence appendix),
            # so a caller attaching working material has to be able to see that it did, and fix it.
            "include_in_report": artifact.include_in_report,
        }), 201


# ── 9b. GET/POST /engagements/<id>/artifacts[/<artifact_id>] — the review surface ────────────────────


def _machine_artifact_dict(a: Artifact) -> dict:
    return {
        "id": a.id,
        "url": artifact_url(a.id),
        "finding_id": a.finding_id,
        "kind": a.kind.value,
        "placement": a.placement.value,
        "filename": a.filename,
        "content_type": a.content_type,
        "byte_size": a.byte_size,
        "caption": a.caption or "",
        "include_in_report": a.include_in_report,
        "created_by": a.created_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@machine_bp.get(f"/engagements/<{_ID}:engagement_id>/artifacts")
@host.require_scope("read")
def scribble_list_artifacts(engagement_id: int):
    """List the engagement's evidence — the REVIEW surface for what a report is about to publish.

    This exists because of what ext#40 changed. Evidence attached to the engagement itself
    (``finding_id`` null) used to reach no deliverable at all, so "unattached" was in practice "not in the
    report"; now the Evidence appendix publishes it, and an operator needs to be able to SEE that set
    before sending the deliverable rather than discover it by reading the rendered report. There was no
    such surface: the cookie API lists a FINDING's artifacts (``GET .../findings/<id>/artifacts``), which
    by construction cannot show a row whose ``finding_id`` is null, and there is no UI for them either.

    ``?unattached=1`` narrows to exactly the engagement-level rows the appendix publishes; the default is
    every artifact on the engagement. Each row carries ``include_in_report``, so what does and does not
    ship is visible in one call, and ``POST`` to the per-artifact route below changes it.
    """
    actor = host.actor()
    unattached_only = (request.args.get("unattached") or "").strip().lower() in _TRUE_WORDS
    with open_session() as db:
        engagement = db.get(Engagement, engagement_id)
        # Missing and not-visible are the SAME 404 — no existence oracle (as everywhere in this module).
        if engagement is None or not can_view_engagement(engagement, actor):
            return _engagement_not_found()
        rows = sorted(engagement.artifacts, key=lambda a: (a.order_index, a.id))
        if unattached_only:
            rows = [a for a in rows if a.finding_id is None]
        out = [_machine_artifact_dict(a) for a in rows]
    return jsonify({"artifacts": out, "count": len(out)})


@machine_bp.post(f"/engagements/<{_ID}:engagement_id>/artifacts/<{_ID}:artifact_id>")
@host.require_scope("write")
@request_body(UpdateArtifactRequest)
def scribble_update_artifact(engagement_id: int, artifact_id: int):
    """Change whether one artifact ships (``include_in_report``) and/or its ``caption``.

    The other half of the review surface, and PAT-reachable on purpose: the cookie route
    (``POST <url_prefix>/api/artifacts/<id>``) needs a session cookie and CSRF, so an agent that had just
    attached working material at engagement level — which the Evidence appendix now publishes — had no way
    to take it back out of the deliverable it had created.

    The artifact is addressed THROUGH its engagement so authorization is the one predicate this module
    uses everywhere (``can_view_engagement``) rather than a second, artifact-shaped rule: an id belonging
    to another engagement answers 404 whatever the caller's grants are, so this route cannot be used to
    probe or edit evidence outside the engagement named in the URL.
    """
    actor = host.actor()
    payload = request.get_json(silent=True) or {}
    with open_session() as db:
        engagement = db.get(Engagement, engagement_id)
        if engagement is None or not can_view_engagement(engagement, actor):
            return _engagement_not_found()
        # Parsed AFTER the tenancy gate, deliberately: a caller with no grant on this engagement gets the
        # one answer this module ever gives it (404) and never a 400 about its own body, which would be the
        # same reply whether or not the engagement exists and reads as "the id is fine, fix your body".
        # Both parsers are pure -- no DB, no disk -- so the ordering costs nothing to have the right way
        # round, and authz-before-body is the discipline the rest of the blueprint follows.
        publish, bad_publish = _include_in_report_or_400(payload.get("include_in_report"))
        if bad_publish is not None:
            return bad_publish
        caption, bad_caption = _opt_str(payload, "caption")
        if bad_caption is not None:
            return bad_caption
        artifact = db.get(Artifact, artifact_id)
        if artifact is None or artifact.engagement_id != engagement_id:
            return jsonify({"error": "not_found", "detail": "artifact not found"}), 404
        if publish is not None:
            artifact.include_in_report = publish
        if "caption" in payload:
            artifact.caption = caption
        db.commit()
        out = _machine_artifact_dict(artifact)
    return jsonify(out)


# ── wiring hook ──────────────────────────────────────────────────────────────────────────────────────


def register(machine_bp_: Blueprint) -> None:  # noqa: ARG001 - routes are already bound above at import
    """No-op: unlike the other feature modules (which receive a shared ``api_bp``/``bp`` created
    elsewhere and attach routes to it lazily, guarded against double-registration), this module owns
    ``machine_bp`` itself and binds its routes to it via decorators at import time — the same pattern
    ``scribble/api.py`` uses for its own module-level ``api_bp``. Kept as a callable purely so
    ``scribble/__init__.py::_wire_feature_routes`` can invoke every feature hook uniformly.
    """
