from __future__ import annotations

import functools
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask, has_request_context, jsonify, request
from sqlalchemy import create_engine, event

import scribble
from scribble import models as _scribble_models
from scribble.seed import seed_defaults


@pytest.fixture
def app(tmp_path):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)

    # Enforce foreign keys in tests too (SQLite defaults OFF) so referential-integrity guards are real.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    cfg = scribble.register(app, engine, instance_path=str(tmp_path), base_template="scribble/base.html")
    with cfg.session_factory() as session:
        seed_defaults(session)
        session.commit()
    return app


def _stamp_core_engagement_id(_mapper, _connection, target):
    """Give every engagement a fixture builds directly the anchor a real one always has.

    This makes the harness match production rather than being kinder than it. Every engagement a
    deployment creates goes through a create path that obtains a CORE engagement first — evidence has
    to be filed under one (`objects.engagement_id` is NOT NULL; INV-OBJSTORE-01) — so a row without an
    anchor is not a state the product produces. Around thirty fixtures construct `Engagement(...)`
    straight, bypassing that path, and each would otherwise carry a shape prod never has.

    Registered at IMPORT time, not from a fixture. It was a function-scoped autouse fixture first, and
    that is too late for the module-scoped `live_app` fixtures in the Playwright suites: higher-scoped
    fixtures are built FIRST, so their demo engagements were inserted before the listener existed and
    every upload in those modules then failed with no anchor.

    The unanchored case is NOT thereby untested: `test_artifact_object_store` drives it directly and
    asserts persisting raises rather than quietly finding somewhere else to put the bytes.
    """
    if getattr(target, "core_engagement_id", None) is None:
        target.core_engagement_id = uuid.uuid7()


event.listen(_scribble_models.Engagement, "before_insert", _stamp_core_engagement_id)


@pytest.fixture(autouse=True)
def _every_app_gets_a_host_object_store(request):
    """Scribble persists evidence ONLY to the host's object store, so every app under test has one.

    Standalone Scribble is a testbed, not a deployment: keeping a local-disk fallback "for standalone"
    would put back the split this cutover deleted (some evidence in the bucket, some on whichever host
    served the upload -- a difference that produced bugs nothing went red for). So the shell that boots
    it supplies a mock host instead, and there is one code path everywhere.

    Autouse and additive: several modules override the `app` fixture with their own, and each of them
    would otherwise need to remember this. `wire_mock_host` only defaults `host`/`pat_actor`, so a
    richer stub host layered on top keeps its own authorization hooks.
    """
    if "app" not in request.fixturenames:
        return
    from scribble.testing import wire_mock_host
    wire_mock_host(request.getfixturevalue("app").extensions["scribble"])


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(app):
    return app.extensions["scribble"].session_factory


@pytest.fixture
def clean_vuln_map(session_factory):
    """Purge the builtin `ScribbleVulnMap` seed (`scribble.seed.seed_vuln_map`, applied by the base
    `app` fixture's `seed_defaults()`) so a test exercising VulnMap CRUD/resolution in isolation can't
    collide with the shipped lotek entries (`dalfox`, `enum4linux`, `kerberoast:` prefix, …) — mirrors
    the deleted lotek `tests/test_api_v1_vulnmap.py`'s `purge_vuln_map=True` default. The builtin seed
    itself is exercised by its own test (`tests/test_vuln_map_seed.py`), not by these."""
    from sqlalchemy import delete

    from scribble.models import ScribbleVulnMap

    with session_factory() as db:
        db.execute(delete(ScribbleVulnMap))
        db.commit()


# ── stub HostServices — a fake mounting host, for machine-route / tenancy / authz tests ────────────
#
# The real host (lotek) injects a `HostServices` bundle into `cfg.extras` AFTER `register()` returns
# (CONTRACT.md §1.3's injection-ordering trap) — see `app/extensions.py::_inject_host`. This fixture
# replicates that same `extras` shape (`host`, `current_actor`, `can_write`, `collab_authorize`,
# `findings`, `require_pat_scope`, `pat_authenticate`, `pat_actor`, `resolve_asset`,
# `mark_job_promoted`) with fakes, so scribble's OWN tests can exercise its machine blueprint
# (`scribble/api_pat.py`), its report authz (`report_html_api._authorize_engagement_view`), and its
# `current_actor`-attributed UI routes (`engagement_ui.py`) WITHOUT importing or booting lotek.
#
# Scope/role RBAC (whether a Bearer token has "write" scope, whether its owning user was demoted) is
# entirely the HOST's own concern — already exercised end-to-end against the real host in
# `tests/test_extension_machine_prefix.py`/`test_host_findings_contract.py` (the lotek repo). So
# `require_pat_scope`/`pat_authenticate` here are deliberately NO-OP passthroughs: scribble's tests
# exist to prove scribble's OWN logic (tenancy pass-through, promote/aggregation, facts→variable
# mapping, CRUD), not to re-prove the host's auth scheme.


@dataclass(frozen=True)
class StubActor:
    """PatActor-shaped fake (see `app/host_contract.py::PatActor`) for the machine blueprint."""

    id: int
    username: str = "admin"
    role: str = "admin"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"read", "write"}))


class _StubRole:
    """UserRole-shaped fake (`app/models.py::UserRole`) for `current_actor()`/report authz."""

    def __init__(self, name: str):
        self.name = name

    def is_admin(self) -> bool:
        return self.name == "admin"

    def can_write(self) -> bool:
        return self.name != "viewer"


@dataclass
class StubUser:
    """The BROWSER-session identity (`scribble.deps.current_actor()`), distinct from `StubActor` (the
    machine/PAT identity) — real lotek keeps these two concepts separate too (session cookie vs Bearer
    token), so this fixture mirrors that rather than collapsing them into one."""

    id: int
    username: str = "admin"
    role: _StubRole = field(default_factory=lambda: _StubRole("admin"))


@dataclass
class FakeFindingDTO:
    """A `host_contract.FindingDTO`-shaped fake — every field the real DTO carries, defaulted so a test
    only fills in what it cares about. `facts` is the declarative-engine's output (`FindingDTO.facts`,
    CONTRACT-FACTS §2/§3): callers set it directly to simulate a module's declared facts, exactly as
    `HostFindings._finding_dto` would have produced it."""

    # A CORE finding id: an int on a legacy/standalone host, a `uuid.UUID` under lotek v2. Annotated as
    # both so the harness is no kinder than the real host -- a test may hand either shape through.
    id: int | uuid.UUID
    job_id: str = "job-1"
    title: str = "Untitled"
    category: str | None = None
    source: str = "autorecon"
    severity: str = "medium"
    confidence: str = "medium"
    status: str = "new"
    dedupe_key: str | None = None
    description: str | None = None
    remediation: str | None = None
    references: list = field(default_factory=list)
    cve: str | None = None
    cvss_score: float | None = None
    analyst_notes: str | None = None
    evidence: str | None = None
    asset_identifier: str | None = None
    target_host: str | None = None
    facts: dict = field(default_factory=dict)


class StubFindings:
    """Fake `host_contract.HostFindings` — a tiny in-memory job/finding registry with the SAME tenancy
    contract the real one enforces internally (`user_can_view_job`): a missing job and a job the actor
    may not view are INDISTINGUISHABLE (`None`/`[]`), never a leak. Tests drive scenarios via
    `add_job(job_id, owner_id=..., dtos=[...])` and by swapping `StubHost.actor` between requests."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def add_job(self, job_id: str, *, owner_id: int | None = None, dtos: Any = ()) -> None:
        self._jobs[job_id] = {"owner_id": owner_id, "dtos": list(dtos)}

    def _visible(self, owner_id: int | None, actor: StubActor | None) -> bool:
        if actor is None:
            return False
        if actor.role == "admin":
            return True
        return owner_id is not None and owner_id == actor.id

    def get_job(self, job_id: str, actor: StubActor | None):
        job = self._jobs.get(job_id)
        if job is None or not self._visible(job["owner_id"], actor):
            return None
        return SimpleNamespace(id=job_id, promoted_extension=None, promoted_ref_id=None)

    def list_findings(self, job_id: str, actor: StubActor | None) -> list:
        job = self._jobs.get(job_id)
        if job is None or not self._visible(job["owner_id"], actor):
            return []
        return list(job["dtos"])

    def get_finding(self, finding_id: int, actor: StubActor | None):
        for job in self._jobs.values():
            for dto in job["dtos"]:
                if dto.id == finding_id:
                    return dto if self._visible(job["owner_id"], actor) else None
        return None  # not registered under any job -> "missing", same shape as "unauthorized"


class StubHost:
    """The test-control object handed back by the `stub_host` fixture: mutate `.actor`/`.current_user`/
    `.can_write_value` between requests (same mounted app, different simulated caller), inspect
    `.promoted_calls` after a promote-job POST, and register jobs/findings on `.findings`."""

    def __init__(self) -> None:
        self.findings = StubFindings()
        self.promoted_calls: list[tuple[str, Any, str, int]] = []
        # Reverse index for `list_jobs` (core #632's host hook): the jobs promoted INTO an engagement,
        # keyed by `(extension, ref_id)`. Tests register via `add_promoted_job(ref_id, job_ref, ...)`;
        # the real host derives this from `Job.promoted_extension`/`.promoted_ref_id`.
        self._promoted_jobs: dict[tuple[str, Any], list] = {}
        # Forward index: which `(extension, ref_id)` each job is CURRENTLY promoted into (the
        # `Job.promoted_*` cols themselves). `mark_job_promoted` reads it to REFUSE-ON-CONFLICT — a job
        # already promoted elsewhere returns False, mirroring core #632 — and writes it on a fresh adopt
        # so `list_jobs` reflects the link a mark just made (one write feeds one read, like production).
        self._promotion_of: dict[str, tuple[str, Any]] = {}
        self.actor: StubActor | None = StubActor(id=1, username="admin", role="admin")
        self.current_user: StubUser | None = StubUser(id=1, username="admin")
        self.can_write_value = True
        # Operator capability on a CORE engagement -- what a caller supplying its own
        # `core_engagement_id` is checked against. Flip it to drive the refusal.
        self.can_operate_value = True
        # Clients this NON-ADMIN actor may read. Mirrors the host's real rule
        # (`app/access.py::user_can_view_client`): admin reads any client, a non-admin reads a client it
        # owns a job under, and a NULL client_id is admin-only. Held as a set here because the stub has
        # no jobs table to derive it from -- but the SHAPE of the answer is production's, deliberately:
        # a stub that granted engagement-OWNER reads would be kinder than the host and would hide the
        # very defect this capability exists to fix.
        self.viewable_client_ids: set[int] = set()
        # Captured `_audit(...)` calls, in order: (action, kwargs). Unwired here would mean the same
        # thing it did before ext#63 fixed the emitting side — `_audit`'s `host.host_hook("audit")`
        # returns None and every call is a silent no-op, which is exactly how the missing audit rows
        # shipped unnoticed.
        self.audit_calls: list[tuple[str, dict]] = []

    def audit(self, db, action: str, **kwargs) -> None:  # noqa: ARG002 - db unused by the stub
        self.audit_calls.append((action, kwargs))

    def mark_job_promoted(self, job_id: str, actor: StubActor | None, *, extension: str, ref_id: int) -> bool:
        """Core #632's writer, faithfully: REFUSE-ON-CONFLICT (a job already promoted into a DIFFERENT
        target returns False, no write) but IDEMPOTENT re-affirm of the SAME target (True, no-op). On a
        fresh adopt it records the link so `list_jobs` returns it, exactly as the real `Job.promoted_*`
        cols feed the reverse view. `promoted_calls` still logs EVERY attempt (incl. the refused one)."""
        self.promoted_calls.append((job_id, actor, extension, ref_id))
        current = self._promotion_of.get(job_id)
        if current is not None and current != (extension, ref_id):
            return False  # promoted elsewhere -> refuse, never silently re-point
        if current is None:
            self.add_promoted_job(ref_id, job_id, extension=extension)  # write the link
        return True

    def add_promoted_job(self, ref_id, job_ref: str, *, extension: str = "scribble", promoted_at=None):
        """Register a job as already promoted INTO `ref_id`, so `list_jobs` returns it (the reverse
        of `mark_job_promoted`) AND `mark_job_promoted` sees it as promoted (refuse-on-conflict).
        `promoted_at` defaults to now; a test that asserts on the rendered timestamp should pass a fixed
        value. Returns the job-shaped object (`.id`, `.promoted_at`)."""
        from datetime import UTC, datetime
        job = SimpleNamespace(id=job_ref, promoted_at=promoted_at or datetime.now(UTC))
        self._promoted_jobs.setdefault((extension, ref_id), []).append(job)
        self._promotion_of[job_ref] = (extension, ref_id)
        return job

    def list_jobs(self, _actor, *, extension: str, ref_id) -> list:
        """Core #632's reverse-view host hook: jobs promoted into `(extension, ref_id)`. Tenancy
        (`user_can_view_job`) is the real host's concern and proven mounted; the stub returns the
        registered index (`_actor` unused — the engagement view is already authorized upstream)."""
        return list(self._promoted_jobs.get((extension, ref_id), []))

    def can_view_client(self, client_id: int | None, actor: Any | None = None) -> bool:
        """The host's client-scoped read gate (`app/extensions.py` injects the real one).

        Absent this key the extension `abort(404)`s by design -- which is exactly what shipped before
        the host provided it: every mounted report 404'd for every actor, admin included.

        Takes EITHER principal shape, because the real one does (`make_can_view_client` is duck-typed on
        `.id` for exactly this reason): a `StubUser` from `current_actor()` carries a `_StubRole`, while a
        `StubActor` from `pat_actor()` carries a plain role STRING. A stub that only understood the
        session shape would `AttributeError` the moment a machine route asked it a tenancy question --
        i.e. it would have made the machine-route fix untestable rather than proving it.
        """
        user = actor if actor is not None else self.current_user
        if user is None:
            return False
        role = getattr(user, "role", None)
        is_admin = role.is_admin() if hasattr(role, "is_admin") else str(role) == "admin"
        if is_admin:
            return True
        if client_id is None:
            return False  # nothing to attribute -> admin-only, mirroring the host's NULL default
        return client_id in self.viewable_client_ids


def _make_stub_idempotent():
    """A faithful in-memory port of lotek's `app/idempotency.py::make_idempotent`, for `extras['idempotent']`.

    This stub did NOT exist until #114, and its absence is why the extension suite could not catch that
    bug: with no `idempotent` extra, `api_pat._with_idempotency` fails open and calls `produce()`
    directly, so every "honours Idempotency-Key" claim in this suite was vacuous while the MOUNTED
    behaviour silently duplicated rows. A stub proves logic, never the mount — but a stub that is KINDER
    than production proves nothing at all, so the branch that actually broke is reproduced exactly:

      * a non-2xx, or a body `json.dumps` cannot serialize, RELEASES the claim so a retry re-executes.
        A raw `uuid.UUID` in the body is exactly such a body (that was the bug).
      * same key + same request fingerprint -> the STORED response is replayed.
      * same key + a DIFFERENT request -> 422, nothing created, nothing replayed.
    """
    store: dict[tuple[Any, str], dict[str, Any]] = {}

    def _fingerprint() -> str:
        endpoint = path_args = ""
        payload = b""
        if has_request_context():
            endpoint = request.endpoint or ""
            path_args = repr(sorted((request.view_args or {}).items(), key=lambda kv: kv[0]))
            payload = (request.get_data() or b"")[:65536]
        return hashlib.sha256(
            f"{endpoint}\x1f{path_args}\x1f{hashlib.sha256(payload).hexdigest()}".encode()
        ).hexdigest()

    def idempotent(principal, key, produce):
        # The real host scopes the slot to the principal's UUID and treats an unresolvable one as
        # "not idempotent". `StubActor.id` is a plain int here, so the slot is scoped to whatever the
        # principal's `.id` is — same SCOPING property, without demanding the fixtures mint UUID actors.
        principal_id = getattr(principal, "id", principal)
        if not key or principal_id is None:
            return produce()
        slot = (principal_id, str(key)[:255])
        fingerprint = _fingerprint()
        claimed = store.get(slot)
        if claimed is not None:
            if claimed["fingerprint"] != fingerprint:
                return {
                    "error": "unprocessable_entity",
                    "detail": "this Idempotency-Key was already used for a different request; "
                              "use a new key for a new operation",
                }, 422
            if claimed["status"] is None:
                # Claimed but not yet answered: the original is STILL RUNNING. The host answers 409 and
                # never reclaims the slot ("old" cannot be told from "slow", and slow is exactly when
                # clients retry). Without this branch the stub fell through to `({}, None)` — a status of
                # None, which `jsonify(...), None` would hard-error on — so it was BOTH kinder than
                # production and incapable of exercising the one refusal a fast-retrying agent actually
                # meets. Found by adversarial review, in the one stub branch that was still generous.
                return {
                    "error": "conflict",
                    "detail": "a request with this Idempotency-Key is still in progress",
                }, 409
            return claimed["body"] or {}, claimed["status"]
        store[slot] = {"fingerprint": fingerprint, "body": None, "status": None}
        try:
            body, status = produce()
        except Exception:
            del store[slot]  # the op failed — release the claim, don't poison the key
            raise
        try:
            storable = len(json.dumps(body)) <= 65536
        except (TypeError, ValueError):
            storable = False
        if 200 <= status < 300 and storable:
            store[slot].update(body=body, status=status)
        else:
            del store[slot]
        return body, status

    return idempotent


def _wire_stub_host(cfg, stub: StubHost) -> None:
    """Fill `cfg.extras` the same way `app/extensions.py::_inject_host` does, with `stub`'s fakes."""

    def require_pat_scope(_scope: str):
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)  # no-op: scope/role RBAC is the HOST's own concern

            return wrapper

        return decorator

    def pat_authenticate():
        return None  # no-op success — see module docstring banner

    def pat_actor():
        return stub.actor

    def current_actor():
        return stub.current_user

    def can_write():
        return stub.can_write_value

    cfg.extras["host"] = stub  # truthy marker `report_html_api._authorize_engagement_view` checks
    cfg.extras["current_actor"] = current_actor
    cfg.extras["can_write"] = can_write
    cfg.extras["collab_authorize"] = lambda finding_id, block: can_write()  # noqa: ARG005
    cfg.extras["findings"] = stub.findings
    cfg.extras["require_pat_scope"] = require_pat_scope
    cfg.extras["pat_authenticate"] = pat_authenticate
    cfg.extras["pat_actor"] = pat_actor
    cfg.extras["resolve_asset"] = lambda session, identifier: None  # noqa: ARG005
    cfg.extras["mark_job_promoted"] = stub.mark_job_promoted
    cfg.extras["list_jobs"] = stub.list_jobs
    cfg.extras["can_view_client"] = stub.can_view_client
    # `_inject_host` provides this and the stub did not, so any code path that consults it saw a
    # fail-closed False and refused. Same shape as the missing `objects` field: a harness that claims
    # to mirror the bundle and is one key short.
    cfg.extras["can_operate_on"] = lambda _engagement_id: stub.can_operate_value
    cfg.extras["audit"] = stub.audit
    cfg.extras["idempotent"] = _make_stub_idempotent()


def install_scope_enforcing_gate(app, stub_host) -> None:
    """Replace `_wire_stub_host`'s NO-OP ``require_pat_scope`` with one that REALLY checks the actor's scopes.

    Scope RBAC is the host's concern, but WHICH scope each route declares is scribble's — and that is only
    provable if a read-only token is actually refused by a write route. Under the no-op stub every machine
    route looks correctly gated even with its ``@host.require_scope`` decorator missing or naming the wrong
    scope: the same "harness kinder than production" hole that made #114's idempotency bug invisible here.

    Mirrors the host (`app/api_v1.py::require_scope`): the token must carry the scope. The host's second
    clause — a ``write`` scope cannot out-rank a viewer-role OWNER — is deliberately not reproduced; it is
    the host's own rule over its own user table, and no scribble route can influence it.

    Lives in conftest because three modules need it (`test_machine_authoring`,
    `test_machine_findings_crud`, `test_scribble_machine_tenancy`). It was copied into the first two with a
    note saying a shared fixture would couple them; a third copy is where copies start to disagree, and
    this is a fixture of the HARNESS, not of any one module's route set.
    """
    cfg = app.extensions["scribble"]

    def require_pat_scope(scope: str):
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                actor = stub_host.actor
                if actor is None or scope not in actor.scopes:
                    return jsonify({"error": "forbidden", "detail": f"scope {scope} required"}), 403
                return fn(*args, **kwargs)

            return wrapper

        return decorator

    cfg.extras["require_pat_scope"] = require_pat_scope


@pytest.fixture
def stub_host(app) -> StubHost:
    """Wire a `StubHost` into the already-mounted `app` fixture's `cfg.extras`, then return the control
    object so a test can register jobs/findings and swap the current actor between requests."""
    cfg = app.extensions["scribble"]
    stub = StubHost()
    _wire_stub_host(cfg, stub)
    return stub
