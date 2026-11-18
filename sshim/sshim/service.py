"""sshim domain logic — Flask-free, the ONE home for every derived-state predicate and the tenancy rule.

Both surfaces (the browser blueprint and the PAT machine API) call THESE functions, so a rule has exactly
one implementation and cannot drift between them:

* **Tenancy** (:func:`_can_see` / the owner filter): a profile/session is visible to its owner and to an
  admin, nobody else (INV-TENANCY-06 explicit declared rule — no engagement_id). A row the caller may
  not see is returned as ``None`` and rendered 404, never 403 (INV-TENANCY-01, no existence oracle).
* **Liveness** lives in :mod:`sshim.plan` (``effective_status`` / ``is_listable``) — the single place
  that decides whether a pivot is live; ``expired`` is derived, never stored.
* **Regeneration** (:func:`plan_for`): a stored session re-renders to byte-identical commands via
  ``plan.rebuild_session`` — no secret needed, because the password was never on any command.

This module raises :class:`Denied` (authz -> 403) and :class:`Invalid` (validation -> 400); it never
raises for "not found" — callers get ``None`` and decide the status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from sshim import models, plan

#: How many sessions a single list query returns. A pivot is disposable and TTL-bounded, so this is a
#: generous ceiling, not pagination.
LIST_LIMIT = 500


class Denied(Exception):
    """Authorization refusal -> 403. Message is sshim's own text, safe to show the caller."""


class Invalid(Exception):
    """Validation failure -> 400. Message is sshim's own text, safe to show the caller."""


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


def _clean_host(value: str | None, *, field: str) -> str | None:
    value = (value or "").strip() or None
    if value is not None and len(value) > models.MAX_HOST:
        raise Invalid(f"{field} is too long (max {models.MAX_HOST})")
    return value


def profile_from_fields(
    *,
    name: str | None,
    topology: str | None,
    rendezvous_host: str | None = None,
    rendezvous_port=22,
    rendezvous_user: str | None = "sshim",
    anchor_host: str | None = None,
    anchor_port=22,
    anchor_user: str | None = "sshim",
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
        rendezvous_user=(rendezvous_user or "sshim").strip()[: models.MAX_USER] or "sshim",
        anchor_host=_clean_host(anchor_host, field="anchor_host"),
        anchor_port=_clean_port(anchor_port, field="anchor_port"),
        anchor_user=(anchor_user or "sshim").strip()[: models.MAX_USER] or "sshim",
        cidr=(cidr or "").strip()[: models.MAX_CIDR],
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

def mint_session(
    db,
    prof: plan.Profile,
    *,
    owner_id: uuid.UUID | None,
    standalone: bool,
    ttl_minutes: int,
    max_ttl: int,
    profile_id: uuid.UUID | None = None,
    host_audit=None,
) -> tuple[models.Session, str]:
    """Mint a pivot: validate, pick random ports + a one-time secret, store the REGENERABLE fields.

    Returns ``(session_row, one_time_password)``. The password is **shown once and not stored** — the
    row carries no secret, and :func:`plan_for` re-renders the commands without it. Clamps ttl to
    ``max_ttl`` (an outward capability's lifetime is a security bound)."""
    if not standalone and owner_id is None:
        raise Denied("no resolvable identity to own this session")
    ttl = min(max(1, int(ttl_minutes)), max_ttl)
    # mint + render validates the topology/field combo (raises ValueError -> Invalid).
    sess = plan.mint(prof, ttl_minutes=ttl)
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
        status=plan.STATUS_ACTIVE,
        params=plan.profile_params(prof),
    )
    db.add(row)
    db.flush()  # assign row.id before auditing
    if host_audit is not None:
        host_audit(
            db, "ext:sshim:mint_session",
            subject_type="sshim_session", subject_id=str(row.id),
            before=None, after={"topology": row.topology, "ttl_minutes": ttl},
        )
    db.commit()
    return row, sess.rendezvous_password


def visible_sessions(db, *, owner_id, is_admin, now: datetime | None = None) -> list[models.Session]:
    """Every pivot the caller may see that is still LISTABLE (active + recently-died within grace, so a
    TTL death is visible instead of a mystery 404)."""
    now = now or now_utc()
    stmt = select(models.Session)
    clause = _owner_clause(models.Session, owner_id=owner_id, is_admin=is_admin)
    if clause is not None:
        stmt = stmt.where(clause)
    # The listable window is a single predicate in plan; mirror its lower bound in SQL, then filter
    # exactly through plan.is_listable so the two never disagree.
    stmt = stmt.where(models.Session.expires_at > now - plan.LISTABLE_GRACE)
    stmt = stmt.order_by(models.Session.expires_at.desc()).limit(LIST_LIMIT)
    rows = list(db.execute(stmt).scalars())
    return [r for r in rows if plan.is_listable(r.expires_at, now=now)]


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


def plan_for(row: models.Session) -> plan.Plan:
    """Regenerate the full plan (both legs, every applicable renderer) from the stored session.

    Secret-free: ``plan.rebuild_session`` rebuilds with an empty password and ``render_plan`` never puts
    it on a command, so the output is byte-identical to what was shown at mint."""
    sess = plan.rebuild_session(
        row.params, socks_port=row.socks_port, jump_port=row.jump_port, ttl_minutes=row.ttl_minutes
    )
    return plan.render_plan(sess)


# ── serialisation for the machine API ─────────────────────────────────────────

def session_to_dict(row: models.Session, *, now: datetime | None = None) -> dict:
    now = now or now_utc()
    return {
        "id": str(row.id),
        "profile_id": str(row.profile_id) if row.profile_id else None,
        "profile_name": row.profile_name,
        "topology": row.topology,
        "rendezvous_host": row.rendezvous_host,
        "socks_port": row.socks_port,
        "jump_port": row.jump_port,
        "ttl_minutes": row.ttl_minutes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat(),
        "status": plan.effective_status(row.status, row.expires_at, now=now),
    }


def plan_to_dict(p: plan.Plan) -> dict:
    """The full re-fetchable plan payload: both legs, every renderer, the notes. No secret — the
    password is never in a command, and the UI/caller re-prompts for it."""
    return {
        "anchor": dict(p.anchor),
        "traveler": dict(p.traveler),
        "notes": list(p.notes),
        "secret_available": False,
        "secret_note": (
            "The relay secret is shown only once at mint and is never stored. Paste your rendezvous "
            "credential at the ssh prompt, or re-mint for a fresh session."
        ),
    }
