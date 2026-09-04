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
import json
import uuid
from collections.abc import Callable
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request
from sqlalchemy import select

from scribble import findings_service, host
from scribble.api_schemas import (
    AddFindingRequest,
    BulkMoveFindingsRequest,
    CreateEngagementRequest,
    CreateGroupRequest,
    CreateTemplateRequest,
    LinkAttackPathRequest,
    MoveFindingRequest,
    PatchEngagementRequest,
    PatchFindingRequest,
    ReorderGroupsRequest,
    UpdateArtifactRequest,
    UpdateAttackPathRequest,
    UpdateGroupRequest,
    UploadArtifactRequest,
    idempotent_route,
    request_body,
)
from scribble.artifacts_api import _as_uuid, artifact_url
from scribble.artifacts_storage import (
    SAFE_NAME_MAX,
    artifact_bytes,
    delete_file,
    guess_content_type,
    persist_bytes,
)
from scribble.authz import (
    can_view_client_id,
    can_view_engagement,
    host_is_mounted,
    visible_engagements,
)
from scribble.content import schema
from scribble.deps import open_session, severity_enum
from scribble.enums import ArtifactKind, ArtifactPlacement, Confidence, FindingStatus, OrderMode
from scribble.models import (
    Artifact,
    Engagement,
    EngagementDiagram,
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

# Every id in this module's routes is Flask's built-in ``<uuid:...>`` converter (lotek#335 --
# Scribble's PKs are UUIDv7, not sequential ``Integer``s). Unlike the bare ``<int:...>`` converter
# it replaced, a malformed value never matches the rule at all: Werkzeug answers a routing 404 --
# the correct response for "no such id" -- and no view runs, so there is no overflow/DataError
# class of bug left to guard against here.

# Upper bound on a linked diagram's self-contained HTML snapshot (see scribble_link_attack_path). Vector's
# export.html inlines its own assets, so it can run larger than a screenshot; this table's ``embed_html``
# is a ``Text`` column held whole in memory on every report render (like ``content_json`` — see
# ``_CONTENT_BLOCK_MAX``'s comment for why persistent, per-render costs get their own explicit cap rather
# than relying on the host's ``MAX_CONTENT_LENGTH`` alone), so a bound is owed here too. 10 MiB is well
# above any real diagram snapshot and far below where either cost bites.
_MAX_DIAGRAM_HTML_BYTES = 10 * 1024 * 1024
_DIAGRAM_REF_MAX_LEN = 64      # EngagementDiagram.diagram_ref  String(64)
_DIAGRAM_CAPTION_MAX_LEN = 255  # EngagementDiagram.caption      String(255)

# ── pure helpers (moved verbatim from the deleted src/app/api_v1_scribble.py) ────────────────────────


def _enum_value(value) -> str | None:
    """The ``.value`` of an enum column, or the raw value when it is already a plain string (a mounted
    host may inject its own ``Severity``, and SQLAlchemy hands some columns back as ``str``). None-safe.
    Was ``_sev_value``; generalized when the findings serializers below needed the same unwrap for
    ``confidence``/``status``/``order_mode``/``kind`` rather than four more copies of it."""
    return getattr(value, "value", value) if value is not None else None


def _opt_uuid(data: dict, key: str):
    """Validate an OPTIONAL Scribble row id — a UUID since lotek#335.

    Scribble's own primary keys became UUIDv7 to remove trivial enumeration, so the id fields a machine
    caller sends (`template_id`, `group_id`, …) are UUID strings over JSON. Parsed as integers they
    produced a flat 400 for every well-formed request, which is how this is caught if it regresses.

    Returns `(value_or_None, error_response_or_None)` so a malformed id is a clean 400 rather than a 500
    raised deep inside `db.get`. Accepts any spelling `uuid.UUID` does (dashed, undashed, braced, mixed
    case) and normalises to a real `uuid.UUID`; rejects everything else, `bool` explicitly included.
    """
    v = data.get(key)
    if v is None:
        return None, None
    bad = (jsonify({"error": "bad_request", "detail": f"{key} must be a UUID"}), 400)
    if isinstance(v, uuid.UUID):
        return v, None
    if isinstance(v, bool) or not isinstance(v, str):
        return None, bad
    try:
        return uuid.UUID(v.strip()), None
    except (ValueError, AttributeError):
        return None, bad


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


def _resolve_engagement(db, raw_id, actor):
    """Address an engagement by EITHER id space (#49): its own UUIDv7 PK, or the core host's engagement
    id it was created with (``Engagement.core_engagement_id`` — int on a legacy/standalone host, UUID on
    v2 core; see models.py).

    Returns the ``Engagement`` if it exists AND ``can_view_engagement`` allows ``actor`` to see it,
    else ``None`` — callers translate a ``None`` to ``_engagement_not_found()``, so this introduces no
    new oracle: unknown id, malformed id, and "exists but not visible" all collapse to the same 404.

    Since lotek#335 every Scribble PK — and every v2 core id — is a UUID, so the ``<uuid:engagement_id>``
    route converter parses the segment before this runs; a non-UUID never matches the rule and 404s at
    routing. A caller therefore cannot address a row by a legacy INTEGER ``core_engagement_id`` through
    the URL any more (that shape is unreachable via a UUID converter), which is an accepted consequence
    of the migration — v2 core keys engagements on UUIDs regardless. A non-UUID reaching here anyway
    (e.g. an internal caller) simply returns ``None`` -> the same 404, rather than raising.
    """
    text = str(raw_id).strip()
    try:
        key = uuid.UUID(text)
    except (ValueError, AttributeError):
        return None
    # A UUID may be EITHER Scribble's own PK (the id Scribble hands back — #49 primary addressing) OR the
    # core host's engagement id it was created with (#49 secondary). Try the PK first, then fall back to
    # ``core_engagement_id``. No UNIQUE constraint on ``core_engagement_id`` (models.py: index, not
    # unique — a pre-existing table can't be retrofitted with one), so a deliberate/accidental collision
    # resolves deterministically to the oldest row rather than 500ing on ``scalar_one``;
    # ``can_view_engagement`` below still gates what the resolved row exposes.
    eng = db.get(Engagement, key)
    if eng is None:
        stmt = (
            select(Engagement)
            .where(Engagement.core_engagement_id == key)
            .order_by(Engagement.id)
            .limit(1)
        )
        eng = db.execute(stmt).scalar_one_or_none()
    if eng is None or not can_view_engagement(eng, actor):
        return None
    return eng


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


# U+2400 SYMBOL FOR NULL — a visible control PICTURE no scan tool emits, so the substitution below can never
# be mistaken for a tool's literal output (a tool can legitimately print the four characters "\x00").
_NUL_SYMBOL = "\u2400"


def _nul_safe(value: str) -> str:
    """Replace NUL (0x00) with ``\u2400``, appending a marker saying how many — core's ``nul_safe`` rule.

    Postgres REFUSES a bind containing NUL (``psycopg`` raises "A string literal cannot contain NUL (0x00)
    characters"), so an otherwise-valid ``PATCH`` carrying one answers **500** for what is a bad request.
    SQLite stores it without complaint, which is why this extension's own suite cannot see the failure — the
    identical blind spot ``_COLUMN_MAX_LEN`` exists for, and the reason both are checked in code instead of
    trusted to a green run. Scan-tool output is exactly where a stray NUL comes from (a partial write, a
    UTF-16 artefact, a raw banner), and ``POST …/promote-job`` is what puts that text in front of the agent
    that then PATCHes it into ``analyst_notes``.

    ESCAPED, not deleted, and not refused: deleting bytes would make a client's evidence silently differ from
    what the tool emitted, and a 400 would leave an agent unable to file a finding over a byte it does not
    control. The count survives in the marker and the substitution is visible in the report.

    A local mirror of core's ``app/text_safety.py::nul_safe`` (same symbol, same marker), duplicated rather
    than imported because scribble must boot standalone with no host to import from — see its docstring for
    the incident that produced it. Applied BEFORE any length cap, so the marker cannot push a value past the
    cap unnoticed.
    """
    n = value.count("\x00")
    if not n:
        return value
    plural = "" if n == 1 else "s"
    return (
        value.replace("\x00", _NUL_SYMBOL)
        + f" \u2026[{n} NUL byte{plural} replaced with {_NUL_SYMBOL}]"
    )


def _opt_str(data: dict, key: str):
    """Validate an OPTIONAL string JSON field -> (stripped_value_or_None, error_response_or_None). A
    non-string (list/dict/number) yields a clean 400 instead of an AttributeError on .strip().

    Every string this blueprint accepts funnels through here or through ``_parse_finding_patch`` (PATCH does
    its own parsing to tell "absent" from "explicit null"), so those two are where ``_nul_safe`` is applied —
    the boundary mirrors core's api_v1 on NUL bytes as well as on length.
    """
    v = data.get(key)
    if v is None:
        return None, None
    if not isinstance(v, str):
        return None, (jsonify({"error": "bad_request", "detail": f"{key} must be a string"}), 400)
    return (_nul_safe(v).strip() or None), None


def _bad_request(detail: str):
    return jsonify({"error": "bad_request", "detail": detail}), 400


# Column-width caps, enforced at the boundary on EVERY route that writes these columns — NOT decoration.
# These are ``String(n)`` columns, so on Postgres an over-long value raises
# ``StringDataRightTruncation`` and the caller gets a 500 for what is really a 400. SQLite stores it
# silently, which is exactly why this needs to be checked in code rather than trusted to "the tests pass":
# the extension's own suite runs on SQLite and cannot see the failure (the same trap INV-INTEGRITY-03's
# uuid/Integer bug hid behind). ``analyst_notes`` is ``Text`` and is deliberately absent — it has no width
# to exceed. Mirrors how core's own api_v1 length-bounds every string it accepts.
#
# Named ``_COLUMN_``, not ``_PATCH_``: it started as a PATCH-only guard, which left the CREATE route on the
# same blueprint writing the same columns unbounded — a 500 waiting on prod behind a green SQLite suite.
# A cap that only one of two writers consults is not a boundary.
_COLUMN_MAX_LEN = {
    "title": 512,          # EngagementFinding.title       String(512)
    "category": 255,       # …category                     String(255)
    "cvss_vector": 255,    # …cvss_vector                  String(255)
    "target_host": 255,    # …target_host                  String(255)
    "target_port": 16,     # …target_port                  String(16)
    "target_url": 1024,    # …target_url                   String(1024)
}
# Same-named fields on OTHER tables of this blueprint, named separately because ``name`` is three
# different widths depending on which route is writing it (see ``_too_long``'s ``cap`` argument).
_GROUP_NAME_MAX_LEN = 128       # FindingGroup.name              String(128)
_ENGAGEMENT_NAME_MAX_LEN = 255  # Engagement.name                String(255)
_SCOPE_TYPE_MAX_LEN = 64        # Engagement.scope_type          String(64)
_COMPANY_NAME_MAX_LEN = 255     # Engagement.company_name        String(255)
_TEMPLATE_NAME_MAX_LEN = 512    # VulnerabilityTemplate.name     String(512)
# NOT the column width: ``Artifact.filename`` is String(512), but the FILESYSTEM binds first.
# ``artifacts_storage.save_bytes`` writes the bytes under "<uuid4hex>_<secure_filename>", so the
# basename is 33 characters longer than the name the caller sent and overruns ``NAME_MAX`` (255 on
# Linux/ext4) at 223 — measured, not assumed: 222 stores, 223 raises ``ENAMETOOLONG`` and the caller
# gets a 500. Cap at the SMALLER of the two limits, or the guard would still 500 on everything between.
# ``SAFE_NAME_MAX`` (== 222) is imported rather than recomputed so this number and the one
# ``save_bytes`` truncates the SECURED name to (artifacts_storage._bounded_name, applied AFTER
# ``secure_filename`` — which NFKD-normalizes and can EXPAND, not just shrink, the caller's input)
# can never drift apart. This cap alone does not stop the filesystem overrun by itself (a 222-char
# unicode name can still secure_filename to 400+ chars) — ``_bounded_name`` is what actually
# protects the write; this 400 just gives the caller an honest, fast rejection for the case its
# own input is unreasonable on its face.
_ARTIFACT_FILENAME_MAX_LEN = SAFE_NAME_MAX

# Bound on a client-supplied ID LIST (``finding_ids`` on the bulk move, ``order`` on the group reorder). The
# length caps above bound one string; this bounds the one input whose LENGTH costs work per element, which is
# the amplification the caps alone left open: 20,000 nonexistent ids in ``finding_ids`` measured **12.7s** of
# database round trips before the request could be refused (one ``db.get`` per id), and ``MAX_CONTENT_LENGTH``
# defaults to 256 MiB on the host, so a ~7 MB body carried ~1M ids — ten minutes of a gevent worker and its
# DB connection, for a write-scoped PAT with membership on ONE engagement. The per-id query is gone (one
# ``select … WHERE id IN (…)`` now), but a cap is still owed: ``place_finding`` re-derives the destination's
# display order for every finding placed, so a bulk move is O(N²) in the CPU regardless of query count.
# 500 is far above any real multi-select drag or agent re-organisation and far below where either cost bites.
_BULK_ID_LIST_MAX = 500

# Bounds on the CONTENT one write may author. ``_COLUMN_MAX_LEN`` bounds one string and
# ``_BULK_ID_LIST_MAX`` bounds one id list; these bound the two content inputs whose LENGTH costs work per
# element AND whose cost is PERSISTENT — they land in ``content_json``, so every later render of that
# finding (HTML and docx, cookie board and machine report alike) walks them again. Measured on this branch
# before the caps existed: a 204 KB ``PATCH`` carrying 5,000 ``content_json`` blocks answered **200** and
# stored 5,001 blocks, one ``render_block`` per block; a ``references`` list of 200,000 entries answered
# **200** and stored **22.2 MB** into a single finding's ``content_json``. That is the same class review
# round 2 closed for ``finding_ids`` — and it was still open one field over, on a route this branch adds.
#
# Applied to every writer that reaches ``_author_content_json`` (``PATCH …/findings/<id>``,
# ``POST …/findings``, ``POST /templates``) for ``_COLUMN_MAX_LEN``'s reason: a check only one of several
# writers consults is not a boundary.
#
# The numbers are far above any real deliverable — ``schema.DEFAULT_BLOCKS`` is three, a custom block is
# named by a human in the editor, and a finding citing 500 references does not exist. Deliberately NOT a
# byte cap on a single block's prose: a long write-up is legitimate, ``analyst_notes`` is an unbounded
# ``Text`` column by design, and the request body is already bounded by the host's ``MAX_CONTENT_LENGTH``.
# What is bounded here is per-element work that outlives the request.
_CONTENT_BLOCK_MAX = 64
_REFERENCE_LIST_MAX = 500


def _too_long(key: str, value: str, *, cap: int | None = None):
    """400 when a value would overflow its column — see ``_COLUMN_MAX_LEN``. None when it fits.

    ``cap`` overrides the table above for a field whose NAME is not unique across this blueprint: ``name``
    is ``String(128)`` on a group, ``String(255)`` on an engagement and ``String(512)`` on a template, so a
    single lookup keyed on the JSON field name would silently apply one table's width to another's column.
    """
    cap = cap if cap is not None else _COLUMN_MAX_LEN.get(key)
    if cap is not None and len(value) > cap:
        return _bad_request(f"{key} too long (max {cap} characters)")
    return None


def _parse_target_fields(data: dict):
    """The three ``target_*`` overrides a CREATE body may carry -> ``(supplied_values, error_or_None)``.

    All three authoring branches of ``scribble_add_finding`` apply these, and all three used to read
    ``data[key]`` raw: no type check (a dict bound straight to a ``String`` column), no width check (a
    600-char title's twin problem — 201 on SQLite, ``StringDataRightTruncation`` 500 on prod Postgres), and
    ``target_port`` coerced with ``str()`` in one branch but not the other two, so an integer port bound as
    an integer to a ``String(16)`` column. One parse, shared, with the same caps ``PATCH`` enforces.

    Only keys the body actually supplied appear in the result; a ``null`` (or a value that strips to empty)
    is treated as "not supplied", which is create's existing semantics — there is nothing to clear on a row
    that does not exist yet.
    """
    supplied: dict[str, str] = {}
    for key in ("target_host", "target_url"):
        if data.get(key) is None:
            continue
        value, err = _opt_str(data, key)
        if err:
            return None, err
        if value is None:
            continue
        if (err := _too_long(key, value)) is not None:
            return None, err
        supplied[key] = value
    port = data.get("target_port")
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, (int, str)):
            return None, _bad_request("target_port must be a string or an integer")
        text = str(port).strip()
        if text:
            if (err := _too_long("target_port", text)) is not None:
                return None, err
            supplied["target_port"] = text
    return supplied, None


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
        # The host's audit `subject_id` is a string column, and callers pass a mix of shapes — since the
        # UUIDv7 PK cutover (lotek#335 / object-store refactor #130) these are `uuid.UUID` objects, while
        # a route's JSON response serializes the same id as a STRING. Coerce once here so every scribble
        # audit row records the id in the one canonical shape a reader (and the id in the API response)
        # actually matches — otherwise `subject_id` is a UUID that never equals the string an auditor
        # correlates it against. (Fixed the two `test_machine_artifacts` audit assertions.)
        subject_id=None if subject_id is None else str(subject_id),
        before=before,
        after=after,
    )


def _idempotency_key(data: Any) -> str | None:
    """The retry key for a mutating request: the ``Idempotency-Key`` header, else a body ``idempotency_key``
    field. Empty/absent -> None (idempotency is opt-in per request)."""
    body_key = data.get("idempotency_key") if isinstance(data, dict) else None
    header_key = request.headers.get("Idempotency-Key")
    return (header_key or body_key) or None


def _json_safe(body: dict) -> dict:
    """``body`` rendered through the app's JSON provider and back — the exact dict ``jsonify`` would put
    on the wire, but built only from JSON-native types.

    🔴 This is not cosmetic; it is the fix for #114 and it applies to EVERY route on this blueprint.
    The host's idempotency store memoizes a response only if ``json.dumps(body)`` succeeds
    (``app/idempotency.py::_storable``) and otherwise **releases the claim so a retry re-executes**.
    Since the UUIDv7 migration (#36 / lotek#335) every body here carries raw ``uuid.UUID`` values,
    ``json.dumps`` raises ``TypeError`` on those, and so ``Idempotency-Key`` silently became a no-op on
    the whole machine API — a retried ``POST …/attack-paths`` minted a second row and doubled the
    diagram in the deliverable. It went unnoticed because core's own ``/api/v1`` call sites hand the
    seam pre-``str()``-ed ids by hand, and because this package's test stub published no ``idempotent``
    extra at all (so the seam was never exercised here).

    Normalising through ``current_app.json`` rather than ``default=str`` is deliberate: it is the SAME
    provider ``jsonify`` uses, so a first response and its replay are byte-identical. ``default=str``
    would render a ``datetime`` as ``2026-01-31 12:00:00+00:00`` where ``jsonify`` emits an HTTP date —
    a replay that quietly differs from the original is its own bug.
    """
    return json.loads(current_app.json.dumps(body))


def _json_object_or_400(data: Any):
    """``(body, None)`` when the request body is a JSON OBJECT, else ``(None, 400)``.

    ``request.get_json(silent=True) or {}`` returns whatever JSON arrived, and every PATCH here then does
    ``set(data) - {...allowed...}`` to reject unknown fields. Handed a non-dict that is truthy — ``[1,2]``,
    ``123``, ``true`` — ``set()`` raises ``TypeError`` INSIDE the view and the caller gets a 500 for a
    plainly malformed request. (``"hello"`` happened to answer 400 by accident, because a string IS
    iterable, which is exactly how this stayed invisible.) ``_idempotency_key`` already guards itself with
    ``isinstance(data, dict)`` for the same reason; this hoists that rule to the body as a whole so the
    three PATCH routes share ONE implementation of it rather than three.
    """
    if isinstance(data, dict):
        return data, None
    return None, _bad_request("body must be a JSON object")


def _with_idempotency(
    key: str | None, produce: Callable[[], tuple[dict, int]]
) -> tuple[dict, int]:
    """Run ``produce`` (``() -> (body_dict, status)``) through the host idempotency seam when a key AND a
    host are present, so a retried POST replays the stored response instead of executing twice; otherwise
    run it directly. The seam's DB unique constraint (not Python) arbitrates the concurrent-retry race."""
    hook = host.host_hook("idempotent")
    if hook is None or not key:
        return produce()

    def _storable_produce() -> tuple[dict, int]:
        body, status = produce()
        return _json_safe(body), status

    return hook(host.actor(), key, _storable_produce)


# ── report rendering helpers (reused by the machine report route) ────────────────────────────────────


def _inline_url_factory(engagement: Engagement, make_inline_artifact_url) -> Callable[[int], str]:
    """``artifact_url`` for ``build_report_context``: resolves an inline-image node's artifact id to the
    renderer-specific placeholder that bakes in the artifact's storage_path."""
    by_id = {a.id: a.storage_path for a in engagement.artifacts}

    def _url(artifact_id: int) -> str:
        return make_inline_artifact_url(by_id.get(artifact_id))

    return _url


def _non_doc_blocks_error(supplied: Any):
    """400 naming the first block in a ``content_json`` MAPPING whose value is not a ProseMirror ``doc``.

    ``None`` when every value is a doc — and ``None`` for anything that is not a mapping at all, because
    "is the container even an object?" is the CALLER's check: ``PATCH`` refuses a non-object (and an explicit
    ``null``), while a CREATE treats one as "no content supplied". Folding those two different contracts in
    here would silently change create's.

    Why this exists at all: ``sanitize_prosemirror`` replaces anything whose root is not a ``doc`` node with
    an EMPTY document — deliberately, so an untrusted caller cannot smuggle a non-``doc`` root past the
    walker. Validating only the CONTAINER's type therefore made ``PATCH /findings/<id>`` answer **200** for
    ``{"content_json": {"description": "Updated description text"}}`` after replacing the vuln write-up with
    ``{"type": "doc", "content": []}``, and echo the emptied doc back as if that were the edit: a client's
    authored prose destroyed, irreversibly, by a body the cookie twin REFUSES (``autosave_api.autosave_block``
    gates on this exact ``schema.is_doc`` and answers 400). A bare string, or a ``{"type": "paragraph", …}``
    node where a ``doc`` root belongs, is the likeliest mistake an agent makes here — ``content_json`` is the
    ONLY way to write a non-default block (there is no plain-text twin for e.g. ``impact``) and this same
    route takes ``description`` as plain TEXT one key over.

    The container check and this one are the same omission one level apart: the type of the collection was
    guarded, the type of its ELEMENTS was not.
    """
    if not isinstance(supplied, dict):
        return None
    for name, doc in supplied.items():
        if not schema.is_doc(doc):
            return _bad_request(
                f"content_json[{name!r}] must be a ProseMirror doc: "
                "{'type': 'doc', 'content': [...]}"
            )
    return None


def _content_bounds_error(data: dict):
    """400 when a body carries more ``content_json`` blocks or ``references`` entries than any real
    finding does — see ``_CONTENT_BLOCK_MAX`` / ``_REFERENCE_LIST_MAX``. ``None`` when it fits.

    Takes the WHOLE body, not one field, because the two inputs live at the same level of it and every
    caller owes both checks; handing this a single field is how the second one gets forgotten. Counted
    BEFORE either value is walked — ``references`` is ``str()``-coerced per element and each
    ``content_json`` block is sanitized and re-rendered, so the walk IS the work being refused.

    Non-mappings/non-lists are ignored here: their TYPE is the caller's business (``PATCH`` refuses a
    non-object ``content_json`` and a non-list ``references``; a create treats either as "not supplied"),
    and folding those different contracts in here would silently change create's.
    """
    supplied = data.get("content_json")
    if isinstance(supplied, dict) and len(supplied) > _CONTENT_BLOCK_MAX:
        return _bad_request(f"content_json may contain at most {_CONTENT_BLOCK_MAX} blocks")
    refs = data.get("references")
    if isinstance(refs, list) and len(refs) > _REFERENCE_LIST_MAX:
        return _bad_request(f"references may contain at most {_REFERENCE_LIST_MAX} entries")
    return None


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
@idempotent_route
def scribble_create_engagement():
    data = request.get_json(silent=True) or {}
    name, err = _opt_str(data, "name")
    if err:
        return err
    if not name:
        return jsonify({"error": "bad_request", "detail": "name is required"}), 400
    # Same boundary rule as the findings routes: these are String(n) columns, so an over-long value is a
    # 400 here rather than a StringDataRightTruncation 500 from prod Postgres (SQLite would store it).
    # scope_type/company_name go through _opt_str for the type check too — they were read raw from the
    # body, so a dict bound straight to a String column.
    if (err := _too_long("name", name, cap=_ENGAGEMENT_NAME_MAX_LEN)) is not None:
        return err
    scope_type, err = _opt_str(data, "scope_type")
    if err:
        return err
    if scope_type is not None and (err := _too_long(
        "scope_type", scope_type, cap=_SCOPE_TYPE_MAX_LEN
    )) is not None:
        return err
    company_name, err = _opt_str(data, "company_name")
    if err:
        return err
    if company_name is not None and (err := _too_long(
        "company_name", company_name, cap=_COMPANY_NAME_MAX_LEN
    )) is not None:
        return err
    client_id, err = _opt_host_id(data, "client_id")
    if err:
        return err
    # Soft ref to the CORE engagement id (#49) -- optional, int-or-UUID, same parser as client_id. Not a
    # tenancy field: it just records the addressing alias (see _resolve_engagement) for a caller that
    # only holds the core id back from POST /api/v1/engagements.
    core_engagement_id, err = _opt_host_id(data, "core_engagement_id")
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

        # THE ANCHOR. `objects.engagement_id` is NOT NULL for every blob (INV-OBJSTORE-01 makes tenancy
        # a database fact via composite FKs), so a scribble engagement with no core engagement behind it
        # has nowhere in the bucket to put evidence — and the only alternative was the local filesystem,
        # which is the split this cutover exists to delete. Obtained at CREATE time so it is never
        # missing at upload time.
        #
        # A caller MAY supply its own, and that path stays open to a plain operator: creating an
        # engagement is manager-or-admin in the host (establishing tenancy is privileged there and the
        # seam delegates to core's own rule rather than restating it), but pointing at one you already
        # operate is not, and refusing that would lock every operator out of filing evidence.
        if core_engagement_id is not None and not host.can_operate_on(core_engagement_id):
            # Same refusal shape as an unknown client: never confirm which core engagement ids exist.
            return _client_not_found()

    # OUTSIDE the mounted branch on purpose. Storage and authorization are separate host capabilities:
    # a shell can supply an object store without a lotek authorization model (that is exactly what the
    # testbed does), and evidence still needs its anchor there. Gating this on `host_is_mounted()`
    # would leave those deployments creating engagements whose uploads could only fail.
    if core_engagement_id is None:
        try:
            core_engagement_id = host.create_engagement(client_id, name)
        except PermissionError:
            return (
                jsonify({
                    "error": "forbidden",
                    "detail": "creating an engagement requires manager or admin in the host; "
                              "pass core_engagement_id of an engagement you already operate",
                }),
                403,
            )
        except ValueError:
            return (
                jsonify({
                    "error": "conflict",
                    "detail": "an engagement with this name already exists for this client",
                }),
                409,
            )

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            eng = Engagement(
                name=name,
                scope_type=(scope_type or "external"),
                company_name=company_name,
                client_id=client_id,
                created_by=actor.username if actor else None,
                # owner_id is unconditional now: scribble owns Engagement/EngagementFinding outright, so
                # it cannot be older than itself (no more capability-gating on the mounted schema).
                owner_id=actor.id if actor else None,
                core_engagement_id=core_engagement_id,
            )
            db.add(eng)
            db.flush()  # assign the PK so the audit row + response can reference it
            body = {
                "id": eng.id,
                "name": eng.name,
                "core_engagement_id": (
                    str(eng.core_engagement_id) if eng.core_engagement_id is not None else None
                ),
            }
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


@machine_bp.get("/templates/<uuid:template_id>")
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
@idempotent_route
def scribble_create_template():
    data = request.get_json(silent=True) or {}
    name, err = _opt_str(data, "name")
    if err:
        return err
    if not name:
        return jsonify({"error": "bad_request", "detail": "name is required"}), 400
    # Column widths, same reason as everywhere else on this blueprint (see _COLUMN_MAX_LEN): a 400 here
    # instead of a Postgres truncation 500 there. ``name`` is String(512) on a TEMPLATE — wider than an
    # engagement's — hence the explicit cap.
    if (err := _too_long("name", name, cap=_TEMPLATE_NAME_MAX_LEN)) is not None:
        return err
    category, err = _opt_str(data, "category")
    if err:
        return err
    if category is not None and (err := _too_long("category", category)) is not None:
        return err
    cvss_vector, err = _opt_str(data, "cvss_vector")
    if err:
        return err
    if cvss_vector is not None and (err := _too_long("cvss_vector", cvss_vector)) is not None:
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

    if (err := _content_bounds_error(data)) is not None:
        return err
    references = data.get("references")
    references = [str(r) for r in references] if isinstance(references, list) else []
    # description/remediation are packed into content_json blocks and sanitized (references live in the
    # template's own ``references`` column, so they are NOT folded into a content block here).
    # Same element-level check the PATCH route makes: a block whose value is not a ``doc`` would be stored
    # as an EMPTY doc behind a 201, i.e. a template created with the prose silently dropped.
    if (err := _non_doc_blocks_error(data.get("content_json"))) is not None:
        return err
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


@machine_bp.post("/engagements/<uuid:engagement_id>/findings")
@host.require_scope("write")
@request_body(AddFindingRequest)
@idempotent_route
def scribble_add_finding(engagement_id: str):
    actor = host.actor()
    actor_username = actor.username if actor else None
    with open_session() as db:
        # DESTINATION tenancy FIRST — before the body is even parsed. Authorizing ahead of validation
        # keeps the refusal for a foreign engagement identical no matter what the body says, so a caller
        # can't map the id space by diffing 400s against 404s. Addressable by EITHER id space (#49) —
        # see _resolve_engagement; a missing/unauthorized/malformed id all collapse to the same 404.
        engagement = _resolve_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        engagement_id = engagement.id  # normalize to the integer PK for every downstream use below

        data = request.get_json(silent=True) or {}
        template_id, err = _opt_uuid(data, "template_id")
        if err:
            return err
        # _opt_HOST_id, not _opt_int: this is a CORE finding id, and core v2 keys it on UUIDv7. Parsed as
        # an int here, promoting a scan finding was unreachable on every v2 host (int("0198…") -> 400)
        # — the same failure, for the same reason, that _opt_host_id was written for on client_id.
        lotek_finding_id, err = _opt_host_id(data, "lotek_finding_id")
        if err:
            return err
        group_id, err = _opt_uuid(data, "group_id")
        if err:
            return err
        # Type-checked and length-capped ONCE, here, because all three branches below write these same
        # String(n) columns — see _parse_target_fields.
        target_fields, err = _parse_target_fields(data)
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
                **target_fields,
            }
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
            for key, value in target_fields.items():
                setattr(finding, key, value)
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
        if (err := _too_long("title", author_title)) is not None:
            return err
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
        if author_cvss_vector is not None:
            if (err := _too_long("cvss_vector", author_cvss_vector)) is not None:
                return err
        # See ``_non_doc_blocks_error``: without this a supplied block whose value is not a ``doc`` is
        # stored as an EMPTY doc and the 201 reports prose that was never saved.
        if (err := _non_doc_blocks_error(data.get("content_json"))) is not None:
            return err
        if (err := _content_bounds_error(data)) is not None:
            return err
        author_content_json = _author_content_json(data)
        author_group_pk = group.id if group is not None else None
        author_order = len(siblings)
        author_target_host = target_fields.get("target_host")
        author_target_url = target_fields.get("target_url")
        author_target_port = target_fields.get("target_port")

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
@idempotent_route
def scribble_create_vuln_map():
    data = request.get_json(silent=True) or {}
    template_id, err = _opt_uuid(data, "template_id")
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


@machine_bp.post("/engagements/<uuid:engagement_id>/promote-job/<job_id>")
@host.require_scope("write")
def scribble_promote_job(engagement_id: str, job_id: str):
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
        engagement = _resolve_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        engagement_id = engagement.id  # normalize to the integer PK for every downstream use below

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
        # Discoverable mapping (#49): a caller may address this engagement by either id (see
        # _resolve_engagement); surfacing it here means it doesn't have to guess or cross-reference.
        "core_engagement_id": (
            str(engagement.core_engagement_id) if engagement.core_engagement_id is not None else None
        ),
        "scope_type": engagement.scope_type,
        "company_name": engagement.company_name,
        "status": engagement.status,
        # lotek#620: the manual overall-risk override (Severity value or None) + its rationale. NULL =
        # no override (the computed risk_rating ladder stands). Set via PATCH /engagements/<id>.
        "risk_override": engagement.risk_override.value if engagement.risk_override else None,
        "risk_override_rationale": engagement.risk_override_rationale,
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


@machine_bp.get("/engagements/<uuid:engagement_id>")
@host.require_scope("read")
def scribble_get_engagement(engagement_id: str):
    actor = host.actor()
    with open_session() as db:
        # Missing, not-visible, AND unaddressable-by-either-id-space are the SAME 404 — no existence
        # oracle over either id space (#49 — see _resolve_engagement).
        engagement = _resolve_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        summary = _engagement_summary(engagement)
        summary["finding_count"] = len(engagement.findings)
        summary["group_count"] = len(engagement.groups)
        summary["artifact_count"] = len(engagement.artifacts)
    return jsonify(summary)


# ── 8c-bis. PATCH /engagements/<id> — set/clear the manual overall-risk override (lotek#620) ─────────


@machine_bp.patch("/engagements/<uuid:engagement_id>")
@host.require_scope("write")
@request_body(PatchEngagementRequest)
def scribble_update_engagement(engagement_id: str):
    """Set or clear the engagement's MANUAL overall-risk override (lotek#620).

    The computed ``risk_rating`` ladder is never destroyed — this only layers an authored judgement on
    top (the renderers show the override AS the headline with an "assessor-adjusted" marker plus the
    original computed band). ``risk_override`` is ``info|low|medium|high|critical`` and an explicit
    ``null`` clears it; direction is unrestricted (up or down). A set override REQUIRES a non-empty
    ``risk_override_rationale`` — an unreasoned override would read as a computed fact, exactly what the
    report must not do — and clearing the override clears its rationale. Only supplied fields change
    (an omitted field is ``_ABSENT`` = unchanged).
    """
    actor = host.actor()
    data = request.get_json(silent=True) or {}
    _, err = _json_object_or_400(data)
    if err is not None:
        return err

    # Parse each field independently: absent -> unchanged, null -> clear, string -> value. The
    # override/rationale COUPLING (a set override needs a rationale) is checked after the merge below,
    # because it depends on the row's existing values as much as on the body.
    raw_override = data.get("risk_override", _ABSENT)
    override_provided = raw_override is not _ABSENT
    parsed_override = None
    if override_provided and raw_override is not None:
        allowed = "|".join(_enum_value(member) or "" for member in severity_enum())
        if not isinstance(raw_override, str):
            return _bad_request(f"risk_override must be a string ({allowed}) or null")
        try:
            parsed_override = severity_enum()(raw_override.strip().lower())
        except ValueError:
            return _bad_request(f"risk_override must be one of {allowed}")

    raw_rationale = data.get("risk_override_rationale", _ABSENT)
    rationale_provided = raw_rationale is not _ABSENT
    parsed_rationale = None
    if rationale_provided and raw_rationale is not None:
        if not isinstance(raw_rationale, str):
            return _bad_request("risk_override_rationale must be a string or null")
        parsed_rationale = raw_rationale.strip() or None

    # Tenancy + write in one session (mirrors the sibling write routes: require_scope('write') gate +
    # _resolve_engagement visibility; engagements are team-shared, so no per-row operator gate here).
    with open_session() as db:
        engagement = _resolve_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()

        new_override = parsed_override if override_provided else engagement.risk_override
        new_rationale = parsed_rationale if rationale_provided else engagement.risk_override_rationale
        if new_override is None:
            # No override => no dangling rationale. Clearing the band clears its reason.
            new_rationale = None
        elif not (new_rationale or "").strip():
            return _bad_request("risk_override requires a non-empty risk_override_rationale")

        before = {
            "risk_override": _enum_value(engagement.risk_override),
            "risk_override_rationale": engagement.risk_override_rationale,
        }
        engagement.risk_override = new_override
        engagement.risk_override_rationale = new_rationale
        after = {
            "risk_override": _enum_value(engagement.risk_override),
            "risk_override_rationale": engagement.risk_override_rationale,
        }
        _audit(
            db, "update_engagement", subject_type="engagement", subject_id=engagement.id,
            before=before, after=after,
        )
        summary = _engagement_summary(engagement)
        db.commit()
    return jsonify(summary)


# ── 8d. GET /engagements/<id>/report — stream the rendered deliverable (html|docx) ───────────────────


@machine_bp.get("/engagements/<uuid:engagement_id>/report")
@host.require_scope("read")
def scribble_engagement_report(engagement_id: str):
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

    reader = artifact_bytes
    with open_session() as db:
        engagement = _resolve_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        engagement_id = engagement.id  # normalize to the integer PK for the audit row below

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


_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


def _finding_id_or_400(raw) -> tuple[uuid.UUID | None, tuple[Response, int] | None]:
    """``(finding_id, refusal)`` for a caller-supplied ``finding_id``. Exactly one is non-None.

    Absent or empty means "engagement-level evidence" -- a legitimate request (the appendix renders it),
    and the multipart surface submits ``finding_id=""`` for an untouched field, so an empty string must
    not be an error.

    Anything else that ``_as_uuid`` cannot parse is REFUSED here rather than silently treated as "no
    finding_id" -- exactly the class of bug closed for the old int-keyed id (adversarial review,
    2026-08-17): a caller-supplied id that fails to parse must not silently land as engagement-level
    evidence while the 201 asserts ``finding_id_dropped: false`` ("you did not ask for one") about a
    request that plainly did. ``_as_uuid`` itself already rejects every shape that used to need
    individual reasoning under the old ``int()``-based parse (floats, bools, out-of-range ints, non-ASCII
    digit strings) -- there is no coercion path left to close case by case, so this wrapper only has to
    decide what "absent" means and turn a parse failure into a 400.

    A WELL-FORMED id belonging to another engagement (or none at all) is still silently dropped rather
    than refused here -- that case would leak whether the id exists; a malformed one cannot leak anything,
    so there is no reason to be quiet about it.
    """
    if raw is None or raw == "":
        return None, None
    fid = _as_uuid(raw)
    if fid is None:
        return None, (jsonify({"error": "bad_request", "detail": "invalid finding_id"}), 400)
    return fid, None


def _include_in_report_or_400(raw) -> tuple[bool | None, tuple[Response, int] | None]:
    """``(include_in_report, refusal)`` for the caller-supplied publish flag. None means "not specified".

    Strict: this flag decides whether an artifact appears in a CLIENT deliverable, so ``bool(raw)`` --
    under which the string ``"false"`` is True -- is the wrong parse. JSON callers send a real boolean;
    the multipart surface can only send text, so the usual word forms are accepted there and anything
    else is refused rather than guessed at.
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


@machine_bp.post("/engagements/<uuid:engagement_id>/artifacts")
@host.require_scope("write")
@request_body(UploadArtifactRequest)
def scribble_upload_artifact(engagement_id: str):
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
        engagement = _resolve_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        engagement_id = engagement.id  # normalize to the integer PK for every downstream use below

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
        kind_raw = payload.get("kind")
        placement_raw = payload.get("placement")
        idempotency_key = payload.get("idempotency_key")
        fid, bad_fid = _finding_id_or_400(payload.get("finding_id"))
        if bad_fid is not None:
            return bad_fid
        publish, bad_publish = _include_in_report_or_400(payload.get("include_in_report"))
        if bad_publish is not None:
            return bad_publish
        # Type-checked, unlike the form branch (where Werkzeug hands back a str either way): a dict
        # ``filename`` reached ``mimetypes.guess_type`` and a dict ``caption`` bound straight to a Text
        # column, both 500s for a bad request.
        filename, err = _opt_str(payload, "filename")
        if err:
            return err
        filename = filename or "artifact"
        caption, err = _opt_str(payload, "caption")
        if err:
            return err
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

    # An over-long filename is a 500 twice over — ``ENAMETOOLONG`` from the filesystem first, a Postgres
    # truncation of ``Artifact.filename``/``storage_path`` behind it — so it is refused here, for the
    # multipart branch too (``upload.filename`` is just as caller-supplied). See the cap's comment for why
    # it is the filesystem's number and not the column's.
    if (err := _too_long("filename", filename, cap=_ARTIFACT_FILENAME_MAX_LEN)) is not None:
        return err
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

    # The SAME persist call the cookie route makes. `persist_bytes` alone decides where the bytes go.
    #
    # A PermissionError is caught here and answered 403 rather than propagating: letting it out of the
    # route means a 500, and a token that may read an engagement without holding an operator
    # capability on it deserves an honest refusal it can tell apart from a crash.
    try:
        storage_path, sha256, byte_size = persist_bytes(
            core_engagement_id=getattr(engagement, "core_engagement_id", None),
            filename=filename, data=data, content_type=content_type)
    except PermissionError:
        return jsonify({
            "error": "forbidden",
            "detail": "not an operator on this engagement in the host - evidence was not stored",
        }), 403
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
        db.flush()  # populate artifact.id before the audit row references it
        _audit(
            db, "upload_artifact", subject_type="artifact", subject_id=artifact.id,
            after={
                "engagement_id": engagement_id,
                "finding_id": artifact.finding_id,
                "filename": artifact.filename,
                "kind": artifact.kind.value,
                "include_in_report": artifact.include_in_report,
                "sha256": sha256,
            },
        )
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
            # is trustworthy because an unparseable one never reaches here -- ``_finding_id_or_400``
            # refuses it with a 400 before the body is even fully read: if it is None the caller really
            # did ask for engagement-level evidence, so a false ``finding_id_dropped: false`` is not
            # reachable through a malformed value.
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


@machine_bp.get("/engagements/<uuid:engagement_id>/artifacts")
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


@machine_bp.post("/engagements/<uuid:engagement_id>/artifacts/<uuid:artifact_id>")
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
        before = {"include_in_report": artifact.include_in_report, "caption": artifact.caption or ""}
        changed = publish is not None or "caption" in payload
        if publish is not None:
            artifact.include_in_report = publish
        if "caption" in payload:
            artifact.caption = caption
        # Only when the request actually asked to change something -- an empty/no-op body (nothing to
        # toggle, no caption key) must not write a before==after row that says nothing happened; that is
        # audit-log noise indistinguishable from a real (if idempotent) edit.
        if changed:
            _audit(
                db, "update_artifact", subject_type="artifact", subject_id=artifact.id,
                before=before,
                after={"include_in_report": artifact.include_in_report, "caption": artifact.caption or ""},
            )
        db.commit()
        out = _machine_artifact_dict(artifact)
    return jsonify(out)


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
        # READ alias for ``id``, closing a write/read asymmetry that broke real client code (#116): the
        # artifact-upload route ACCEPTS ``finding_id``, so a driver that read a finding back and passed
        # ``f["finding_id"]`` straight to an upload hit a KeyError. Same value, always — ``id`` stays the
        # canonical field and is not going away.
        "finding_id": finding.id,
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


def _patch_content_blocks(data: dict):
    """Sanitized ``{block_name: prosemirror_doc}`` for whatever content a PATCH body carried -> (blocks,
    error). Empty dict when the body carried none, which means "leave the prose alone".

    Runs through the SAME ``_author_content_json`` the create route uses, so a PATCH cannot become a
    second, laxer path into ``content_json`` — the sanitizer is the stored-XSS gate for every write-scoped
    caller and it must not be reachable around. Types are validated strictly here (rather than relying on
    ``_author_content_json``'s isinstance checks, which silently IGNORE a wrong-typed field) because on an
    edit route a silently-dropped block reports success for prose that was never saved.

    An EMPTY value is that same failure by another route, and it is handled here for the same reason.
    ``_author_content_json``'s guards are truthiness tests (``if isinstance(text, str) and text.strip()``,
    ``if refs_text``), written for a CREATE where "nothing supplied" and "supplied nothing" are the same
    thing. On an edit they are not: ``{"description": ""}`` is the only way to say "delete this block's
    prose", and letting the guard swallow it meant a 200 for an edit that never happened — with no way at
    all to clear a block through this API. So a supplied-but-empty ``description``/``remediation``/
    ``references`` becomes an explicit empty ProseMirror doc, which is exactly what the cookie editor's
    autosave stores for a cleared block (``autosave_api``) and what the renderer treats as an absent
    section. A block named in ``content_json`` still wins over its plain-text twin, unchanged.

    ``{"content_json": {}}`` is NOT a clear-everything: it supplies zero blocks, so it changes no prose (and
    on its own it is the "no updatable fields supplied" 400). Clearing is per block, by name.
    """
    if not (set(_PATCH_CONTENT_FIELDS) & data.keys()):
        return {}, None
    supplied = data.get("content_json", _ABSENT)
    if supplied is not _ABSENT and not isinstance(supplied, dict):
        return {}, _bad_request("content_json must be an object of {block_name: prosemirror_doc}")
    if (err := _non_doc_blocks_error(supplied)) is not None:
        return {}, err
    if (err := _content_bounds_error(data)) is not None:
        return {}, err
    for key in ("description", "remediation"):
        value = data.get(key, _ABSENT)
        if value is not _ABSENT and not isinstance(value, str):
            return {}, _bad_request(f"{key} must be a string")
    refs = data.get("references", _ABSENT)
    if refs is not _ABSENT and not isinstance(refs, list):
        return {}, _bad_request("references must be a list")
    blocks = _author_content_json({k: data[k] for k in _PATCH_CONTENT_FIELDS if k in data})
    # Whatever the caller named but ``_author_content_json`` produced no block for was empty — clear it.
    # Routed through the sanitizer too, so there is still exactly one path into ``content_json``.
    cleared = {
        key: schema.empty_doc()
        for key in ("description", "remediation", "references")
        if key in data and key not in blocks
    }
    if cleared:
        blocks = {**blocks, **sanitize_content_json(cleared)}
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
    data, bad_body = _json_object_or_400(data)
    if bad_body is not None:
        return None, None, bad_body
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
        updates["title"] = _nul_safe(title).strip()
        if (err := _too_long("title", updates["title"])) is not None:
            return None, None, err

    for key in _PATCH_TEXT_FIELDS:
        value = data.get(key, _ABSENT)
        if value is _ABSENT:
            continue
        if value is None:
            updates[key] = None
            continue
        if not isinstance(value, str):
            return None, None, _bad_request(f"{key} must be a string or null")
        updates[key] = _nul_safe(value).strip() or None
        if updates[key] is not None and (err := _too_long(key, updates[key])) is not None:
            return None, None, err

    port = data.get("target_port", _ABSENT)
    if port is not _ABSENT:
        if port is None:
            updates["target_port"] = None
        elif isinstance(port, bool) or not isinstance(port, (int, str)):
            return None, None, _bad_request("target_port must be a string, an integer, or null")
        else:
            updates["target_port"] = _nul_safe(str(port)).strip() or None
            if updates["target_port"] is not None:
                if (err := _too_long("target_port", updates["target_port"])) is not None:
                    return None, None, err

    score = data.get("cvss_score", _ABSENT)
    if score is not _ABSENT:
        if score is None:
            updates["cvss_score"] = None
        elif isinstance(score, bool) or not isinstance(score, (int, float)):
            return None, None, _bad_request("cvss_score must be a number or null")
        elif not (0.0 <= float(score) <= 10.0):
            # Bounded to the CVSS range, which also refuses the ``NaN``/``Infinity`` tokens Python's JSON
            # parser accepts by default — both are floats, both survive to the column, and both render
            # into a client's deliverable as a severity number that means nothing.
            return None, None, _bad_request("cvss_score must be between 0.0 and 10.0")
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


@machine_bp.get("/engagements/<uuid:engagement_id>/findings")
@host.require_scope("read")
def scribble_list_findings(engagement_id: int):
    """Every finding in an engagement, in BOARD order — the flat list the drag board shows, NOT the nested
    document tree the report renders.

    ``GET /engagements/<id>`` answers a bare ``finding_count``, so before this route a machine caller
    could not read back what it had created, let alone address it by id. Group order is
    ``FindingGroup.order_index``; within a group, ``findings_service.display_order`` applies the group's
    own ``order_mode`` — the same ordering ``reporting/context.py`` reads.

    It is the BOARD list on purpose, and the distinction matters twice:

      * A promoted per-host CHILD appears here as its own entry (with its ``parent_id`` set), because that
        is where it sits on the board and because ``order_index`` is a slot in exactly THIS list — the one
        ``place_finding`` counts positions in. Nesting the listing would have made the move route's
        indices refer to a list the caller could no longer see.
      * The RENDERED report nests those children inside their parent's card, so ``count`` (board rows) is
        not the number of findings a client sees. ``top_level_count`` is that number — the renderer's own
        rule (``findings_service.rendered_top_level_count``), excluded groups and excluded findings
        dropped. Quote that one; a 1-parent/2-child promotion is ONE finding in the deliverable and three
        rows here.
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
            "top_level_count": findings_service.rendered_top_level_count(engagement),
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


@machine_bp.get("/findings/<uuid:finding_id>")
@host.require_scope("read")
def scribble_get_finding(finding_id: int):
    actor = host.actor()
    with open_session() as db:
        finding = _visible_finding(db, finding_id, actor)
        if finding is None:
            return _finding_not_found()
        return jsonify(_finding_detail(db, finding))


# ── 10c. PATCH /findings/<id> — edit in place ────────────────────────────────────────────────────────


@machine_bp.patch("/findings/<uuid:finding_id>")
@host.require_scope("write")
@request_body(PatchFindingRequest)
@idempotent_route
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


@machine_bp.delete("/findings/<uuid:finding_id>")
@host.require_scope("write")
@idempotent_route
def scribble_delete_finding(finding_id: int):
    """Delete a finding and its evidence — ``findings_service.delete_finding``, the same cascade the
    cookie board performs (artifact ROWS go with it; a group delete only detaches, a finding delete does
    not, because a finding IS its content).

    Nested per-host CHILDREN are the exception: they are DETACHED (``parent_id`` -> NULL) and reported in
    ``detached_children``, never deleted. A promoted parent is a synthesized umbrella row over the vuln-DB
    write-up while its children hold the real per-host evidence, so one DELETE must not destroy N findings
    the caller never named — and until ``detach_children`` existed this route answered 500 for every
    promoted parent (the self-FK has no ``ondelete``). The detached ids are in the response because a
    delete that turned other rows into top-level findings must say so.

    The on-disk artifact files are unlinked only AFTER the transaction commits, so a rolled-back delete
    cannot leave the bytes gone. On an idempotent REPLAY nothing is unlinked, because ``_produce`` never
    runs — the files went with the original request.
    """
    actor = host.actor()

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
            deleted = findings_service.delete_finding(db, finding)
            removed_paths.extend(deleted.storage_paths)
            _audit(
                db, "delete_finding", subject_type="finding", subject_id=finding_id,
                before=before,
                # The detached children ARE part of what this delete did, so they belong in the audit row
                # too — otherwise the trail records one row removed and says nothing about the rows it
                # re-parented.
                after=(
                    {"detached_children": deleted.detached_child_ids}
                    if deleted.detached_child_ids
                    else None
                ),
            )
            db.commit()
            return {"deleted": True, "finding_id": finding_id,
                    "engagement_id": authorized_engagement_id,
                    "detached_children": deleted.detached_child_ids}, 200

    body, status = _with_idempotency(_idempotency_key(request.get_json(silent=True) or {}), _produce)
    if status == 200:
        for storage_path in removed_paths:
            delete_file(storage_path)
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

    A NEGATIVE ``order_index`` is refused rather than clamped. ``place_finding`` clamps with
    ``max(0, min(requested, len))``, so every negative offset collapses to slot 0 — and in a BULK move each
    successive insert at slot 0 pushes the previous one down, silently REVERSING the caller's listed order
    (``order_index: -1``, the obvious way to say "before the first", reverses a 2-item move). Zero already
    means "before the first", so a negative index cannot express anything the caller could have meant;
    refusing it is honest where clamping quietly did the opposite of what was asked.
    """
    if "group_id" not in data:
        return None, 0, _bad_request("group_id is required (use null to move to ungrouped)")
    order_index, err = _opt_int(data, "order_index")
    if err:
        return None, 0, err
    order_index = 0 if order_index is None else order_index
    if order_index < 0:
        return None, 0, _bad_request("order_index must be 0 or greater (0 = the first position)")

    if data.get("group_id") is None:
        return None, order_index, None
    group_id, err = _opt_uuid(data, "group_id")
    if err:
        return None, 0, err
    target_group = _group_of(db, engagement_id, group_id)
    if target_group is None:
        return None, 0, _group_not_found()
    return target_group, order_index, None


@machine_bp.post("/findings/<uuid:finding_id>/move")
@host.require_scope("write")
@request_body(MoveFindingRequest)
@idempotent_route
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


@machine_bp.post("/engagements/<uuid:engagement_id>/findings/move")
@host.require_scope("write")
@request_body(BulkMoveFindingsRequest)
@idempotent_route
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
        # Bounded BEFORE the list is walked — see ``_BULK_ID_LIST_MAX``. Counted on the raw list, not on the
        # de-duplicated one, because the work this refuses is the walk itself.
        if len(raw_ids) > _BULK_ID_LIST_MAX:
            return _bad_request(f"finding_ids may contain at most {_BULK_ID_LIST_MAX} ids")
        finding_ids: list[uuid.UUID] = []
        for raw in raw_ids:
            parsed = _as_uuid(raw)
            if parsed is None:
                return _bad_request("finding_ids must contain UUIDs")
            if parsed not in finding_ids:
                finding_ids.append(parsed)

        target_group, order_index, err = _parse_move_target(data, engagement_id, db)
        if err is not None:
            return err
        target_group_id = target_group.id if target_group is not None else None

        # Membership of the URL's engagement is checked for EVERY id before anything moves. Note this
        # cannot leak: the engagement itself was already authorized above, so the only thing an id
        # confirms is whether it is in a report the caller can already read in full.
        #
        # ONE query, not one per id: this loop used to ``db.get`` each id, which turned a single request into
        # N round trips BEFORE it could refuse (measured 12.7s for 20,000 nonexistent ids — the caller pays
        # nothing, the database pays everything). Same refusal, same atomicity: the set difference is empty
        # only if every id belongs to this engagement.
        present = set(db.scalars(
            select(EngagementFinding.id).where(
                EngagementFinding.id.in_(finding_ids),
                EngagementFinding.engagement_id == engagement_id,
            )
        ).all())
        if len(present) != len(finding_ids):
            return _finding_not_found()

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            target_group = (
                _group_of(db, engagement_id, target_group_id) if target_group_id is not None else None
            )
            if target_group_id is not None and target_group is None:
                return {"error": "not_found", "detail": "group not found on this engagement"}, 404
            placed: list[EngagementFinding] = []
            for offset, fid in enumerate(finding_ids):
                finding = db.get(EngagementFinding, fid)
                if finding is None or finding.engagement_id != engagement_id:
                    return {"error": "not_found", "detail": "finding not found"}, 404
                findings_service.place_finding(finding, target_group, order_index + offset)
                placed.append(finding)
            # Read AFTER every placement, not inside the loop: each insert reindexes the whole destination,
            # so an order_index read mid-loop is stale by the time the next finding lands — the response
            # reported numbers that were never persisted (every entry read 0 for a 3-item move).
            moved = [{"finding_id": f.id, "order_index": f.order_index} for f in placed]
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


@machine_bp.post("/engagements/<uuid:engagement_id>/groups")
@host.require_scope("write")
@request_body(CreateGroupRequest)
@idempotent_route
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
    # `FindingGroup.name` is String(128) — see _COLUMN_MAX_LEN for why this is checked here and not left
    # to the database (Postgres 500s, SQLite stores the over-long value silently).
    if (err := _too_long("name", name, cap=_GROUP_NAME_MAX_LEN)) is not None:
        return err
    assessment_type_id, err = _opt_uuid(data, "assessment_type_id")
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


@machine_bp.patch("/engagements/<uuid:engagement_id>/groups/<uuid:group_id>")
@host.require_scope("write")
@request_body(UpdateGroupRequest)
@idempotent_route
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

    data, bad_body = _json_object_or_400(request.get_json(silent=True) or {})
    if bad_body is not None:
        return bad_body
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
        if (err := _too_long("name", name, cap=_GROUP_NAME_MAX_LEN)) is not None:
            return err
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


@machine_bp.delete("/engagements/<uuid:engagement_id>/groups/<uuid:group_id>")
@host.require_scope("write")
@idempotent_route
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


@machine_bp.post("/engagements/<uuid:engagement_id>/groups/reorder")
@host.require_scope("write")
@request_body(ReorderGroupsRequest)
@idempotent_route
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
    # The cheaper sibling of the bulk move's list (no query per element — ``reorder_groups`` walks it in
    # memory), capped by the same constant anyway: an engagement has a handful of sections, so any list this
    # long is a mistake or an attempt, and "cheap per element" is not a bound.
    if len(order) > _BULK_ID_LIST_MAX:
        return _bad_request(f"order may contain at most {_BULK_ID_LIST_MAX} ids")

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


# ── attack-path diagrams (ext#48) ───────────────────────────────────────────────────────────────────


def _attack_path_not_found():
    """Missing diagram, or one belonging to a DIFFERENT engagement — one refusal for both, so the per-item
    routes cannot be used to enumerate diagram ids across engagements (same rule as ``_group_not_found``)."""
    return jsonify({"error": "not_found", "detail": "attack path not found on this engagement"}), 404


def _diagram_of(db, engagement_id, attack_path_id) -> EngagementDiagram | None:
    """A linked diagram BY ID, but only if it belongs to ``engagement_id`` — else None. The diagram-shaped
    twin of ``_group_of``: the per-item routes address a diagram THROUGH its engagement, so the engagement
    the caller was authorized for is the one whose rows it can reach."""
    diagram = db.get(EngagementDiagram, attack_path_id)
    if diagram is None or diagram.engagement_id != engagement_id:
        return None
    return diagram


def _diagram_dict(d: EngagementDiagram) -> dict:
    return {
        "id": d.id,
        "engagement_id": d.engagement_id,
        "diagram_ref": d.diagram_ref,
        "caption": d.caption or "",
        "include_in_report": d.include_in_report,
        "order_index": d.order_index,
        # The snapshot itself is potentially large and is not useful in a listing — a caller that wants
        # it renders the report, or re-fetches vector's export directly. Its presence (not its content)
        # is what a review surface needs, matching artifacts_api leaving byte content out of listings.
        "has_embed_html": bool(d.embed_html),
    }


@machine_bp.post("/engagements/<uuid:engagement_id>/attack-paths")
@host.require_scope("write")
@request_body(LinkAttackPathRequest)
@idempotent_route
def scribble_link_attack_path(engagement_id):
    """Link a vector attack-path diagram into this engagement's report (ext#48).

    Scribble has no seam to reach vector directly (a separate extension, no host hook exposes it), so
    THIS route accepts an already-rendered, self-contained HTML snapshot rather than fetching one: the
    caller GETs vector's ``/vector/machine/diagrams/{id}/export.html`` and POSTs the result here as
    ``embed_html``. The report embeds it verbatim inside a sandboxed iframe
    (``render_html._render_diagram_item``) — this route stores it, never parses or executes it.

    Tenancy is checked BEFORE the body is read (same rule as ``scribble_upload_artifact``, for the same
    reason: a caller must not be able to map the id space by diffing 400s against 404s for an engagement
    it cannot see). ``idempotency_key`` (body or ``Idempotency-Key`` header) makes a retry return the
    original link (200) rather than creating a duplicate.
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()

    data = request.get_json(silent=True) or {}
    embed_html, err = _opt_str(data, "embed_html")
    if err:
        return err
    if not embed_html:
        return _bad_request("embed_html is required")
    if len(embed_html.encode("utf-8")) > _MAX_DIAGRAM_HTML_BYTES:
        return jsonify({
            "error": "payload_too_large",
            "detail": f"embed_html exceeds the {_MAX_DIAGRAM_HTML_BYTES // (1024 * 1024)} MiB limit",
        }), 413

    diagram_ref, err = _opt_str(data, "diagram_ref")
    if err:
        return err
    if (err := _too_long("diagram_ref", diagram_ref or "", cap=_DIAGRAM_REF_MAX_LEN)) is not None:
        return err
    caption, err = _opt_str(data, "caption")
    if err:
        return err
    if (err := _too_long("caption", caption or "", cap=_DIAGRAM_CAPTION_MAX_LEN)) is not None:
        return err
    publish, err = _include_in_report_or_400(data.get("include_in_report"))
    if err:
        return err

    idempotency_key = _idempotency_key(data)

    def _produce() -> tuple[dict, int]:
        with open_session() as wdb:
            eng = wdb.get(Engagement, engagement_id)
            if eng is None:
                return {"error": "not_found", "detail": "engagement not found"}, 404
            siblings = list(eng.diagrams)
            diagram = EngagementDiagram(
                engagement_id=engagement_id,
                diagram_ref=diagram_ref,
                caption=caption,
                embed_html=embed_html,
                order_index=len(siblings),
                include_in_report=publish if publish is not None else True,
            )
            wdb.add(diagram)
            wdb.flush()  # populate diagram.id before the audit row references it
            body = _diagram_dict(diagram)
            # The POST was the ONE route in this trio with no in-band audit row (it shipped that way in
            # ext#48, covered only by core's generic `extension.machine_write` backstop). With update and
            # delete now emitting semantic verbs, "who put this diagram in the deliverable" would have
            # been the only one of the three questions the audit reader could not answer.
            _audit(
                wdb, "link_attack_path", subject_type="engagement_diagram", subject_id=diagram.id,
                after={**body, "engagement_id": engagement_id},
            )
            wdb.commit()
            return body, 201

    body, status = _with_idempotency(idempotency_key, _produce)
    return jsonify(body), status


@machine_bp.get("/engagements/<uuid:engagement_id>/attack-paths")
@host.require_scope("read")
def scribble_list_attack_paths(engagement_id):
    """List the attack-path diagrams linked to this engagement — the review surface for what the
    report's Attack Paths block will publish (mirrors ``scribble_list_artifacts``)."""
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        rows = sorted(engagement.diagrams, key=lambda d: (d.order_index, d.id))
        out = [_diagram_dict(d) for d in rows]
    # ``attack_paths`` is the key that matches the ROUTE; ``diagrams`` is the original one, kept as a
    # duplicate alias so a client written against it does not break in the same release that fixes the
    # name (#116). A client that guessed ``attack_paths`` — the obvious guess, and the one a report driver
    # actually made — got a KeyError and reported "no attack paths" for data that was there. Both keys
    # reference the same list objects, so the duplication costs one pointer per row, not a second
    # serialization.
    #
    # 🔴 DEPRECATED — ``diagrams`` is removed by #121, which is the tracking issue for exactly that and
    # names every site to touch. A relative deadline ("one release") in a comment is a promise nothing
    # enforces; an open issue is a hand that can act. Do not restate a date here — point at #121.
    return jsonify({"attack_paths": out, "diagrams": out, "count": len(out)})


@machine_bp.get("/engagements/<uuid:engagement_id>/attack-paths/<uuid:attack_path_id>")
@host.require_scope("read")
def scribble_get_attack_path(engagement_id, attack_path_id):
    """One linked attack path, INCLUDING its stored ``embed_html`` snapshot.

    The listing omits the snapshot (it is up to 10 MiB per row); this route is where a caller reads it
    back — to diff what was uploaded against what vector currently exports, or to confirm a retry stored
    what it meant to. The diagram is addressed THROUGH its engagement, so tenancy is the engagement's.
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        diagram = _diagram_of(db, engagement_id, attack_path_id)
        if diagram is None:
            return _attack_path_not_found()
        return jsonify({**_diagram_dict(diagram), "embed_html": diagram.embed_html})


@machine_bp.patch("/engagements/<uuid:engagement_id>/attack-paths/<uuid:attack_path_id>")
@host.require_scope("write")
@request_body(UpdateAttackPathRequest)
@idempotent_route
def scribble_update_attack_path(engagement_id, attack_path_id):
    """Edit a linked attack path in place: ``include_in_report`` and/or ``caption``.

    ``include_in_report: false`` is the NON-destructive way to keep a wrongly-linked diagram out of the
    deliverable — the same convention artifacts, findings and groups already use — and is what a caller
    should reach for before ``DELETE`` when the snapshot may still be wanted.
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        if _diagram_of(db, engagement_id, attack_path_id) is None:
            return _attack_path_not_found()

    data, bad_body = _json_object_or_400(request.get_json(silent=True) or {})
    if bad_body is not None:
        return bad_body
    unknown = set(data) - {"caption", "include_in_report", "idempotency_key"}
    if unknown:
        return _bad_request(f"unknown field(s): {', '.join(sorted(unknown))}")
    updates: dict[str, Any] = {}
    if "caption" in data:
        caption, err = _opt_str(data, "caption")
        if err:
            return err
        if (err := _too_long("caption", caption or "", cap=_DIAGRAM_CAPTION_MAX_LEN)) is not None:
            return err
        updates["caption"] = caption
    if "include_in_report" in data:
        # Strict `isinstance(bool)`, matching ``scribble_update_group`` rather than
        # ``_include_in_report_or_400`` (which exists for the MULTIPART artifact surface, where a flag can
        # only arrive as text). That helper maps an explicit ``null``/``""`` to None meaning "not
        # specified", which is right for a CREATE and wrong here: on a PATCH the key IS present, so None
        # would be written to a NOT NULL column and a caller's `{"include_in_report": null}` would come
        # back a 500 instead of the 400 it is.
        if not isinstance(data["include_in_report"], bool):
            return _bad_request("include_in_report must be true or false")
        updates["include_in_report"] = data["include_in_report"]
    if not updates:
        return _bad_request("no updatable fields supplied")

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            diagram = _diagram_of(db, engagement_id, attack_path_id)
            if diagram is None:
                return {"error": "not_found", "detail": "attack path not found on this engagement"}, 404
            before = {field: getattr(diagram, field) for field in updates}
            for field, value in updates.items():
                setattr(diagram, field, value)
            body = _diagram_dict(diagram)
            _audit(
                db, "update_attack_path", subject_type="engagement_diagram", subject_id=diagram.id,
                before=before, after={field: body[field] for field in updates},
            )
            db.commit()
            return body, 200

    body, status = _with_idempotency(_idempotency_key(data), _produce)
    return jsonify(body), status


@machine_bp.delete("/engagements/<uuid:engagement_id>/attack-paths/<uuid:attack_path_id>")
@host.require_scope("write")
@idempotent_route
def scribble_delete_attack_path(engagement_id, attack_path_id):
    """Unlink an attack path from the report and delete its stored snapshot.

    This is the undo the collection lacked (#114): before it, a duplicated link could be removed only
    through the dashboard UI, and a report driver that retried a POST had no way back at all. Deleting
    the row destroys only Scribble's SNAPSHOT — the source diagram lives in vector and is untouched, so
    a delete is recoverable by re-linking. Surviving rows are RE-PACKED to a contiguous ``order_index``,
    because ``order_index`` is a slot in the rendered list (the same rule the board's move routes
    follow) and leaving a hole makes the next link's ``len(siblings)`` collide with an existing slot.
    """
    actor = host.actor()
    with open_session() as db:
        engagement = _visible_engagement(db, engagement_id, actor)
        if engagement is None:
            return _engagement_not_found()
        if _diagram_of(db, engagement_id, attack_path_id) is None:
            return _attack_path_not_found()

    def _produce() -> tuple[dict, int]:
        with open_session() as db:
            diagram = _diagram_of(db, engagement_id, attack_path_id)
            if diagram is None:
                return {"error": "not_found", "detail": "attack path not found on this engagement"}, 404
            before = _diagram_dict(diagram)
            engagement = diagram.engagement
            db.delete(diagram)
            db.flush()
            for index, sibling in enumerate(
                sorted(engagement.diagrams, key=lambda d: (d.order_index, d.id))
            ):
                # Only WRITE a row whose slot actually moved. Rewriting every sibling unconditionally
                # takes a row lock on all of them, and two concurrent deletes on one engagement (each
                # skipping a different victim) then take those locks in different orders — the textbook
                # deadlock shape. ponytail: this narrows the lock set, it does not serialize the two
                # deletes; if concurrent deletes on ONE engagement ever become common, take the
                # engagement row `FOR UPDATE` at the top of both this and the link route's _produce.
                if sibling.order_index != index:
                    sibling.order_index = index
            _audit(
                db, "delete_attack_path", subject_type="engagement_diagram",
                subject_id=attack_path_id, before=before, after=None,
            )
            db.commit()
            return {"deleted": True, "attack_path_id": attack_path_id,
                    "engagement_id": engagement_id}, 200

    body, status = _with_idempotency(
        _idempotency_key(request.get_json(silent=True) or {}), _produce
    )
    return jsonify(body), status


# ── the published contract ───────────────────────────────────────────────────────────────────────────


@machine_bp.get("/openapi.json")
@host.require_scope("read")
def scribble_machine_openapi():
    """The OpenAPI 3.1 document for this machine API — response shapes included.

    lotek core's ``GET /api/v1/openapi.json`` already lists these routes (it keys off the same
    ``require_scope`` stamp) but documents no response bodies, which is the half a client has to guess —
    and guessing it wrong is silent (#116: a driver looked for ``attack_paths``, the payload said
    ``diagrams``, and it reported an uploaded attack path as missing). This one is generated from the
    live ``url_map`` plus the declared response schemas in ``scribble/openapi.py``.

    ``read`` scope, like every other GET here: the document describes an instance's enabled surface, and
    it is the natural first call of a client that already holds a token.
    """
    from scribble import _version, openapi

    return jsonify(openapi.build_spec(
        current_app, machine_bp.name, version=getattr(_version, "__version__", "0")
    ))


# ── wiring hook ──────────────────────────────────────────────────────────────────────────────────────


def register(machine_bp_: Blueprint) -> None:  # noqa: ARG001 - routes are already bound above at import
    """No-op: unlike the other feature modules (which receive a shared ``api_bp``/``bp`` created
    elsewhere and attach routes to it lazily, guarded against double-registration), this module owns
    ``machine_bp`` itself and binds its routes to it via decorators at import time — the same pattern
    ``scribble/api.py`` uses for its own module-level ``api_bp``. Kept as a callable purely so
    ``scribble/__init__.py::_wire_feature_routes`` can invoke every feature hook uniformly.
    """
