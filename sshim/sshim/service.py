"""sshim domain logic — Flask-free, the ONE home for every derived-state predicate and the tenancy rule.

Both surfaces (the browser blueprint and the PAT machine API) call THESE functions, so a rule has exactly
one implementation and cannot drift between them:

* **Tenancy** (:func:`_can_see` / the owner filter): a profile/session is visible to its owner and to an
  admin, nobody else (INV-TENANCY-06 explicit declared rule — no engagement_id). A row the caller may
  not see is returned as ``None`` and rendered 404, never 403 (INV-TENANCY-01, no existence oracle).
* **Liveness** lives in :mod:`sshim.plan` (``effective_status`` / ``is_listable`` / ``dies_at``) — the
  single place that decides whether a pivot is live; ``expired`` is derived, never stored.
* **The stored credential** (:func:`reveal_password`): the one place that opens a session's sealed
  ``ssh_password``, and the one place that decides "no key -> no secret" (fail closed, never garbage).
* **Regeneration** (:func:`plan_for`): a stored session re-renders to byte-identical commands via
  ``plan.rebuild_session``; a session whose status is terminal ``killed`` refuses the plan outright.
* **Reaping** (:func:`sweep_expired` / :func:`maybe_sweep`): there is no core reaper in Phase 1, so
  sshim notices its OWN expiries — one idempotent UPDATE, throttled by one constant.

This module raises :class:`Denied` (authz -> 403), :class:`Invalid` (validation -> 400) and
:class:`SessionDead` (a terminal session -> the plan is refused); it never raises for "not found" —
callers get ``None`` and decide the status.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select, update

from sshim import models, plan, secretbox

#: How many sessions a single list query returns. A pivot is disposable and TTL-bounded, so this is a
#: generous ceiling, not pagination.
LIST_LIMIT = 500


class Denied(Exception):
    """Authorization refusal -> 403. Message is sshim's own text, safe to show the caller."""


class Invalid(Exception):
    """Validation failure -> 400. Message is sshim's own text, safe to show the caller."""


class SessionDead(Exception):
    """This session is terminally killed, so its plan is refused. Message is sshim's own text."""


#: There is no core reaper in Phase 1, so sshim sweeps its OWN expiries from a blueprint
#: ``before_request``. An in-process throttle, not a background thread: the sweep is at most once per
#: this many seconds per worker, so a burst of page loads costs one UPDATE, not one per request. A cron
#: job (``flask sshim-reap``) can force it whenever it likes.
SWEEP_INTERVAL_SECONDS = 30.0
_last_sweep_at = 0.0
_sweep_lock = threading.Lock()


def now_utc() -> datetime:
    """Naive UTC — matches the naive ``DateTime`` columns so ``expires_at`` vs ``now`` never mixes
    aware/naive (which raises). Every liveness comparison in sshim flows through this one clock."""
    return datetime.now(UTC).replace(tzinfo=None)


# ── tenancy — one rule, two representations of it ─────────────────────────────

def _can_see(row, *, owner_id: uuid.UUID | None, is_admin: bool) -> bool:
    """The single tenancy predicate: owner or admin. A ``None`` owner_id (anonymous/unresolved) matches
    no row, which is the fail-closed direction."""
    if is_admin:
        return True
    return owner_id is not None and row.owner_id == owner_id


def _owner_clause(cls, *, owner_id: uuid.UUID | None, is_admin: bool):
    """The SQL mirror of :func:`_can_see` for list queries. Same rule, expressed for the DB."""
    if is_admin:
        return None  # no restriction
    if owner_id is None:
        return cls.id == None  # noqa: E711 — match nothing (fail closed)
    return cls.owner_id == owner_id


# ── validation ────────────────────────────────────────────────────────────────

def _clean_name(name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        raise Invalid("a name is required")
    if len(name) > models.MAX_NAME:
        raise Invalid(f"name is too long (max {models.MAX_NAME})")
    return name


def _clean_port(value, *, field: str, default: int = 22) -> int:
    if value in (None, ""):
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise Invalid(f"{field} must be a number") from None
    if not (1 <= port <= 65535):
        raise Invalid(f"{field} must be 1..65535")
    return port


# A rendezvous/anchor host, an ssh username and a CIDR all end up inside a shell command the operator
# (or a human copying an agent-minted command) pastes into a terminal. So they are a shell-injection
# trust boundary even though sshim never executes them itself: a value like `evil.com; rm -rf /` would
# run on paste. Reject anything outside a conservative charset for each — real hostnames, usernames and
# CIDRs never contain shell metacharacters, so this costs nothing legitimate and closes the chain.
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")      # DNS name or IP literal (colon for IPv6)
_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")       # POSIX-ish ssh username
_CIDR_RE = re.compile(r"^[0-9A-Fa-f.:/]+$")       # IPv4/IPv6 CIDR


def _clean_host(value: str | None, *, field: str) -> str | None:
    value = (value or "").strip() or None
    if value is None:
        return None
    if len(value) > models.MAX_HOST:
        raise Invalid(f"{field} is too long (max {models.MAX_HOST})")
    if not _HOST_RE.match(value):
        raise Invalid(f"{field} may only contain letters, digits, '.', '-', ':'")
    return value


def _clean_user(value: str | None) -> str:
    """A pinned ssh username, or ``""`` for "generate a fresh per-pivot identity".

    Empty is a MEANING here, not a missing value: :func:`plan.rendezvous_identity` reads ``""`` as "no
    pin, use this session's generated user". sshim ships no shared literal user any more."""
    value = (value or "").strip()[: models.MAX_USER]
    if value and not _USER_RE.match(value):
        raise Invalid("user may only contain letters, digits, '.', '-', '_'")
    return value


def parse_kill_at(value) -> datetime | None:
    """An OPT-IN kill date: blank -> ``None``, else a naive-UTC datetime (``datetime-local`` or ISO 8601).

    Same clock as every other liveness comparison (:func:`now_utc`), so ``kill_at`` and ``expires_at``
    can never disagree about timezone. A value in the past is ACCEPTED: it mints an already-killed
    session, which is what an operator pasting a stale date should see, not a silent no-op."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            raise Invalid("kill at must be a date/time (YYYY-MM-DDTHH:MM)") from None
    if dt.tzinfo is not None:  # normalise to naive UTC, the columns' convention
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _clean_cidr(value: str | None) -> str:
    value = (value or "").strip()[: models.MAX_CIDR]
    if value and not _CIDR_RE.match(value):
        raise Invalid("cidr may only contain hex digits, '.', ':', '/'")
    return value


def profile_from_fields(
    *,
    name: str | None,
    topology: str | None,
    rendezvous_host: str | None = None,
    rendezvous_port=22,
    rendezvous_user: str | None = "",
    anchor_host: str | None = None,
    anchor_port=22,
    anchor_user: str | None = "",
    cidr: str | None = "",
) -> plan.Profile:
    """Validate raw form/JSON fields into a ``plan.Profile`` dataclass. Raises :class:`Invalid`."""
    topology = (topology or "").strip()
    if topology not in plan.TOPOLOGIES:
        raise Invalid(f"topology must be one of {', '.join(plan.TOPOLOGIES)}")
    prof = plan.Profile(
        name=_clean_name(name),
        topology=topology,
        rendezvous_host=_clean_host(rendezvous_host, field="rendezvous_host"),
        rendezvous_port=_clean_port(rendezvous_port, field="rendezvous_port"),
        rendezvous_user=_clean_user(rendezvous_user),
        anchor_host=_clean_host(anchor_host, field="anchor_host"),
        anchor_port=_clean_port(anchor_port, field="anchor_port"),
        anchor_user=_clean_user(anchor_user),
        cidr=_clean_cidr(cidr),
    )
    # Topology-specific requirements (rendezvous for A/C, anchor for B/C) — plan owns that rule.
    try:
        plan.render_plan(plan.mint(prof, ttl_minutes=1))
    except ValueError as exc:
        raise Invalid(str(exc)) from None
    return prof


def profile_to_dataclass(row: models.Profile) -> plan.Profile:
    """A stored profile row -> the ``plan.Profile`` the generator consumes."""
    return plan.Profile(
        name=row.name,
        topology=row.topology,
        rendezvous_host=row.rendezvous_host,
        rendezvous_port=row.rendezvous_port,
        rendezvous_user=row.rendezvous_user,
        anchor_host=row.anchor_host,
        anchor_port=row.anchor_port,
        anchor_user=row.anchor_user,
        cidr=row.cidr,
    )


# ── profiles ──────────────────────────────────────────────────────────────────

def create_profile(db, prof: plan.Profile, *, owner_id: uuid.UUID | None, standalone: bool) -> models.Profile:
    if not standalone and owner_id is None:
        raise Denied("no resolvable identity to own this profile")
    row = models.Profile(
        owner_id=owner_id,
        name=prof.name,
        topology=prof.topology,
        rendezvous_host=prof.rendezvous_host,
        rendezvous_port=prof.rendezvous_port,
        rendezvous_user=prof.rendezvous_user,
        anchor_host=prof.anchor_host,
        anchor_port=prof.anchor_port,
        anchor_user=prof.anchor_user,
        cidr=prof.cidr,
    )
    db.add(row)
    db.commit()
    return row


def visible_profiles(db, *, owner_id, is_admin, q: str | None = None) -> list[models.Profile]:
    stmt = select(models.Profile)
    clause = _owner_clause(models.Profile, owner_id=owner_id, is_admin=is_admin)
    if clause is not None:
        stmt = stmt.where(clause)
    if q:
        stmt = stmt.where(models.Profile.name.ilike(f"%{q.strip()}%"))
    stmt = stmt.order_by(models.Profile.name).limit(LIST_LIMIT)
    return list(db.execute(stmt).scalars())


def load_profile_visible(db, profile_id, *, owner_id, is_admin) -> models.Profile | None:
    row = db.get(models.Profile, profile_id)
    if row is None or not _can_see(row, owner_id=owner_id, is_admin=is_admin):
        return None
    return row


def delete_profile(db, row: models.Profile, *, owner_id, is_admin) -> None:
    if not _can_see(row, owner_id=owner_id, is_admin=is_admin):
        raise Denied("not your profile")
    # Sessions minted from this profile keep running — profile_id is ON DELETE SET NULL and they
    # regenerate from their own snapshot. Deleting a profile never tears down a live pivot.
    db.delete(row)
    db.commit()


# ── sessions — mint, list, load, kill, regenerate ─────────────────────────────

#: Topologies that dial a rendezvous — the only ones a container can stand in for. In ``direct_socks``
#: the operator talks to the anchor itself, so there is nothing to host.
RELAY_TOPOLOGIES = frozenset({"relay_reverse_socks", "relay_sshuttle"})

#: The name this extension declared in its manifest with sharing = "exclusive". Core validates
#: the name against that declaration, so it is a constant, not a call-site choice.
RELAY_CONTAINER_NAME = "sshim-relay"

RELAY_CONTAINER = "container"   # a core-hosted single-use relay provisions this session's identity
RELAY_EXTERNAL = "external"     # somebody else's sshd: the account must already exist there


def relay_dial(result) -> tuple[str | None, int | None]:
    """``(host, port)`` from a host ``exclusive_container_acquire`` result, or ``(None, None)``.

    Tolerates both naming conventions the seam may use (``host``/``port`` or ``relay_host``/
    ``public_port``) and any junk: an unusable answer must fall back to Phase-1 behaviour rather than
    mint a pivot pointed at nothing.
    """
    if not isinstance(result, dict):
        return None, None
    host = result.get("host") or result.get("relay_host")
    raw_port = result.get("port") or result.get("public_port")
    try:
        port = int(raw_port) if raw_port is not None else None
    except (TypeError, ValueError):
        port = None
    if not isinstance(host, str) or not host.strip() or not port:
        return None, None
    return host.strip(), port


def relay_mode(params: dict | None) -> str:
    """Which kind of relay a stored session actually rides on (``container`` or ``external``)."""
    if isinstance(params, dict) and params.get("relay_mode") == RELAY_CONTAINER:
        return RELAY_CONTAINER
    return RELAY_EXTERNAL


def relay_handle(params: dict | None) -> str | None:
    """The id the host handed back for this session's exclusive container, if any."""
    if isinstance(params, dict):
        handle = params.get("relay_handle")
        if isinstance(handle, str) and handle.strip():
            return handle.strip()
    return None


def mint_session(
    db,
    prof: plan.Profile,
    *,
    owner_id: uuid.UUID | None,
    standalone: bool,
    ttl_minutes: int,
    max_ttl: int,
    secret_key_path: Path,
    profile_id: uuid.UUID | None = None,
    kill_at: datetime | None = None,
    host_audit=None,
    acquire=None,
) -> tuple[models.Session, str]:
    """Mint a pivot: validate, generate this session's OWN identity + random ports, store both.

    Returns ``(session_row, ssh_password)``. The password IS persisted now — sealed with
    ``secretbox`` under ``secret_key_path`` — because the owner needs to re-fetch it from a different
    borrowed box (:func:`reveal_password`). Nothing cleartext ever reaches the row.

    ``kill_at`` is the OPT-IN extra bound; the TTL is still applied underneath it and is never removed.
    Both are clamped/reduced by :func:`plan.dies_at` at read time, so this function stores what it was
    given and lets the ONE liveness predicate decide."""
    if not standalone and owner_id is None:
        raise Denied("no resolvable identity to own this session")
    ttl = min(max(1, int(ttl_minutes)), max_ttl)
    # mint + render validates the topology/field combo (raises ValueError -> Invalid).
    sess = plan.mint(prof, ttl_minutes=ttl)

    # ── Phase 2: ask the host for a core-hosted single-use relay ────────────────────────────────────
    # The container is what makes this session's random ssh_user/ssh_password REAL: the relay image
    # provisions the account from USER_NAME/USER_PASSWORD. An external rendezvous cannot — the account
    # must already exist there — so the mode is recorded and surfaced rather than implied away.
    relay_state = RELAY_EXTERNAL
    handle: str | None = None
    if acquire is not None and prof.topology in RELAY_TOPOLOGIES:
        # Core owns the relay credential: it creates the container, so it must set USER_NAME/
        # USER_PASSWORD on it. We ADOPT what it returns — a second identity minted here would be
        # provisioned nowhere, which is exactly how this shipped broken the first time.
        try:
            got = acquire(name=RELAY_CONTAINER_NAME, ttl_minutes=ttl)
        except Exception as exc:  # noqa: BLE001 — a refusal must NOT become a silent fallback
            raise Invalid(f"the relay container could not be started: {exc}") from None
        if got is None:
            relay_state = RELAY_EXTERNAL          # no docker on this host: Phase 1 behaviour
        else:
            host, port = relay_dial(got)
            if not host or not port:
                raise Invalid("the relay container returned no usable address")
            core_user = str(got.get("ssh_user") or "").strip()
            core_password = str(got.get("ssh_password") or "")
            # The profile may pin a rendezvous_user (the external-rendezvous case). On the container
            # path CORE's user is authoritative — it is the account that exists inside the container —
            # so it must override the pinned one, not merely sit behind it.
            prof = dataclasses.replace(
                prof,
                rendezvous_host=host,
                rendezvous_port=port,
                rendezvous_user=core_user or prof.rendezvous_user,
            )
            sess = dataclasses.replace(
                sess,
                profile=prof,
                ssh_user=core_user or sess.ssh_user,
                ssh_password=core_password or sess.ssh_password,
            )
            relay_state = RELAY_CONTAINER
            raw = got.get("handle_id") or got.get("session_id") or got.get("container")
            if isinstance(raw, str) and raw.strip():
                handle = raw.strip()

    try:
        plan.render_plan(sess)
    except ValueError as exc:
        raise Invalid(str(exc)) from None

    now = now_utc()
    row = models.Session(
        owner_id=owner_id,
        profile_id=profile_id,
        profile_name=prof.name,
        topology=prof.topology,
        rendezvous_host=prof.rendezvous_host,
        socks_port=sess.socks_port,
        jump_port=sess.jump_port,
        ttl_minutes=ttl,
        expires_at=now + timedelta(minutes=ttl),
        ssh_user=sess.ssh_user,
        ssh_password=secretbox.seal(sess.ssh_password, key_path=secret_key_path),
        kill_at=kill_at,
        status=plan.STATUS_ACTIVE,
        params=_params_with_relay(plan.profile_params(prof), relay_state, handle),
    )
    db.add(row)
    db.flush()  # assign row.id before auditing
    if host_audit is not None:
        # `ssh_user` is audited (it is on argv, it is not a secret); the PASSWORD never is.
        host_audit(
            db, "ext:sshim:mint_session",
            subject_type="sshim_session", subject_id=str(row.id),
            before=None,
            after={"topology": row.topology, "ttl_minutes": ttl, "ssh_user": row.ssh_user},
        )
    db.commit()
    return row, sess.ssh_password


def give_back_relay(row, release) -> bool:
    """Hand this session's container back to the host. Best-effort; returns True if it was released.

    ONE implementation for every teardown path. The first version released only from the dashboard
    kill route, so an agent-killed pivot left its container running — the same forgotten-call-site bug
    the acquire path had. A host that already reaped the container (TTL, admin reap) must not fail the
    caller's kill, so a raising release is logged and swallowed.
    """
    handle = relay_handle(getattr(row, "params", None))
    if not handle or release is None:
        return False
    try:
        release(handle)
        return True
    except Exception as exc:  # noqa: BLE001 — teardown is best-effort, never blocks a kill
        _log.warning("sshim: relay release failed for %s: %s", handle, exc)
        return False


def _params_with_relay(base: dict, mode: str, handle: str | None) -> dict:
    """The stored profile snapshot plus HOW this session's relay is provided.

    Kept in ``params`` so no schema migration is owed, and so a re-fetch can state the mode honestly.
    """
    out = dict(base)
    out["relay_mode"] = mode
    if handle:
        out["relay_handle"] = handle
    return out


def visible_sessions(db, *, owner_id, is_admin, now: datetime | None = None) -> list[models.Session]:
    """Every pivot the caller may see that is still LISTABLE (active + recently-died within grace, so a
    TTL death is visible instead of a mystery 404)."""
    now = now or now_utc()
    stmt = select(models.Session)
    clause = _owner_clause(models.Session, owner_id=owner_id, is_admin=is_admin)
    if clause is not None:
        stmt = stmt.where(clause)
    # The listable window is a single predicate in plan; mirror its lower bound in SQL (either bound can
    # be the one that dies — plan.dies_at is the min), then filter exactly through plan.is_listable so
    # the SQL prefilter and the predicate can never disagree about a row.
    horizon = now - plan.LISTABLE_GRACE
    stmt = stmt.where(
        or_(models.Session.expires_at > horizon, models.Session.kill_at > horizon)
    )
    stmt = stmt.order_by(models.Session.expires_at.desc()).limit(LIST_LIMIT)
    rows = list(db.execute(stmt).scalars())
    return [r for r in rows if plan.is_listable(r.expires_at, now=now, kill_at=r.kill_at)]


def load_visible(db, session_id, *, owner_id, is_admin) -> models.Session | None:
    row = db.get(models.Session, session_id)
    if row is None or not _can_see(row, owner_id=owner_id, is_admin=is_admin):
        return None
    return row


def kill_session(db, row: models.Session, *, owner_id, is_admin, host_audit=None) -> None:
    if not _can_see(row, owner_id=owner_id, is_admin=is_admin):
        raise Denied("not your session")
    before = row.status
    row.status = plan.STATUS_KILLED
    if host_audit is not None:
        host_audit(
            db, "ext:sshim:kill_session",
            subject_type="sshim_session", subject_id=str(row.id),
            before={"status": before}, after={"status": row.status},
        )
    db.commit()


def reveal_password(row: models.Session, *, secret_key_path: Path) -> str | None:
    """The session's password, unsealed — or ``None`` when the key can't open it (FAIL CLOSED).

    The one home for "may this secret be shown": a missing/corrupt instance key or a tampered token is
    reported as "no secret", never as an empty or garbled string, so a surface can offer a re-mint
    instead of pasting nonsense at a password prompt. Callers are already past the tenancy check
    (``load_visible``), i.e. the owner or an admin.
    """
    if not row.ssh_password:
        return None
    try:
        return secretbox.unseal(row.ssh_password, key_path=secret_key_path)
    except secretbox.SecretUnavailable:
        return None


def plan_for(row: models.Session, *, secret_key_path: Path, now: datetime | None = None) -> plan.Plan:
    """Regenerate the full plan (both legs, every applicable renderer) from the stored session.

    The identity flows back from the row: ``ssh_user`` as stored, and the password UNSEALED into the
    returned plan's session (``""`` if the key can't read it) so the owner's re-fetch can show it again.
    ``render_plan`` never puts the password on a command, so the commands stay byte-identical to mint.

    A TERMINALLY KILLED session (stored ``killed`` or a reached ``kill_at``) raises
    :class:`SessionDead` — its commands stop being served as live. A merely TTL-EXPIRED session still
    re-renders (today's behaviour: the page shows what was minted, marked expired)."""
    now = now or now_utc()
    if plan.effective_status(row.status, row.expires_at, now=now, kill_at=row.kill_at) == plan.STATUS_KILLED:
        raise SessionDead("this pivot is killed — its commands are no longer served. Mint a new session.")
    sess = plan.rebuild_session(
        row.params,
        socks_port=row.socks_port,
        jump_port=row.jump_port,
        ttl_minutes=row.ttl_minutes,
        ssh_user=row.ssh_user or "",
        ssh_password=reveal_password(row, secret_key_path=secret_key_path) or "",
    )
    return plan.render_plan(sess)


# ── reaping — no core reaper in Phase 1, so sshim notices its own expiries ───────────────────────

def sweep_expired(db, *, now: datetime | None = None) -> dict:
    """Mark every session past its kill date as terminal, and REPORT the TTL-expired ones.

    Idempotent and safe under concurrent requests by construction: the kill-date arm is ONE
    ``UPDATE ... WHERE kill_at <= now AND status <> 'killed'`` (atomic — two racing requests update the
    same rows, the second matching none), and the TTL arm needs no write at all because ``expired`` is
    DERIVED by :func:`plan.effective_status` and is never stored. So a second sweep is a no-op that
    returns zeroes, and nothing can be "re-killed" or have its expiry rewritten.

    Returns ``{"killed": n, "expired": m}`` — rows this call transitioned, and rows already past their
    TTL (counted, not written)."""
    now = now or now_utc()
    killed = db.execute(
        update(models.Session)
        .where(
            models.Session.kill_at.is_not(None),
            models.Session.kill_at <= now,
            models.Session.status != plan.STATUS_KILLED,
        )
        .values(status=plan.STATUS_KILLED)
    ).rowcount
    expired = len(
        [
            r
            for r in db.execute(
                select(models.Session).where(models.Session.expires_at <= now)
            ).scalars()
            if plan.effective_status(r.status, r.expires_at, now=now, kill_at=r.kill_at)
            == plan.STATUS_EXPIRED
        ]
    )
    db.commit()
    return {"killed": killed or 0, "expired": expired}


def maybe_sweep(db, *, now: datetime | None = None, interval: float = SWEEP_INTERVAL_SECONDS) -> dict | None:
    """Throttled :func:`sweep_expired` for the blueprint's ``before_request`` — ``None`` when skipped.

    One process-local timestamp + lock, no thread and no scheduler: at most one sweep per
    ``interval`` seconds per worker process. The timestamp is taken BEFORE the sweep runs, so a
    failing sweep cannot turn every request into a retry loop."""
    global _last_sweep_at
    tick = time.monotonic()
    with _sweep_lock:
        if tick - _last_sweep_at < interval:
            return None
        _last_sweep_at = tick
    return sweep_expired(db, now=now)


# ── serialisation for the machine API ─────────────────────────────────────────

def session_to_dict(row: models.Session, *, now: datetime | None = None) -> dict:
    """One session, for LIST responses. ⚠️ Never carries ``ssh_password``.

    The list is the wide surface (every running pivot); the credential belongs only to the
    single-session detail its owner opens, so it is deliberately absent here — a caller cannot leak
    what it was never handed."""
    now = now or now_utc()
    return {
        "id": str(row.id),
        "profile_id": str(row.profile_id) if row.profile_id else None,
        "profile_name": row.profile_name,
        "topology": row.topology,
        "rendezvous_host": row.rendezvous_host,
        "socks_port": row.socks_port,
        "jump_port": row.jump_port,
        "ssh_user": row.ssh_user,
        "ttl_minutes": row.ttl_minutes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat(),
        "kill_at": row.kill_at.isoformat() if row.kill_at else None,
        "status": plan.effective_status(row.status, row.expires_at, now=now, kill_at=row.kill_at),
    }


def plan_to_dict(p: plan.Plan) -> dict:
    """The full re-fetchable plan payload: both legs, every renderer, the notes, the pivot's identity.

    The password is included ONLY when the plan carries a successfully unsealed one (owner/admin detail
    and the mint response); ``secret_available`` is false when the instance key could not read it, and
    then no value at all is sent — never an empty or stale string dressed up as a real secret."""
    sess = p.session
    password = sess.ssh_password or None
    return {
        "anchor": dict(p.anchor),
        "operator": dict(p.operator),
        "notes": list(p.notes),
        "ssh_user": sess.ssh_user,
        "secret_available": password is not None,
        "ssh_password": password,
        "secret_note": (
            "The pivot password is stored sealed for this instance and re-shown to its owner here. If "
            "it reads as unavailable the instance key is missing/corrupt: re-mint for a fresh identity."
            if password is not None
            else "The pivot's stored password could not be unsealed on this instance (its key is "
            "missing or corrupt). Re-mint for a fresh identity — do not paste an empty secret."
        ),
    }

_log = logging.getLogger(__name__)
