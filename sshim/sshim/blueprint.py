"""Human-facing UI — the running-pivots list, the mint page, and the re-fetchable session detail.

Every route resolves authorization + liveness through ``sshim.service`` / ``sshim.plan`` — the SAME
functions the PAT machine API uses, so the rules cannot drift between the two surfaces. A session/profile
the caller may not see is **404, never 403** (INV-TENANCY-01, no existence oracle).

The one-time relay secret is shown ONLY on the mint RESPONSE page (rendered directly from POST, never
redirected, never stored, never put in a cookie). The re-fetch surface (GET detail) regenerates the
commands without it and says so.
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, abort, redirect, render_template, request, url_for

from sshim._version import __version__
from sshim.deps import (
    current_actor_id,
    current_actor_is_admin,
    default_relay_host,
    get_config,
    host_audit,
    is_standalone,
    max_ttl_minutes,
)
from sshim.plan import (
    ENCODINGS,
    RENDERERS,
    TOPOLOGIES,
    Profile,
    deviation_risks,
    effective_status,
)
from sshim.service import (
    Denied,
    Invalid,
    create_profile,
    delete_profile,
    kill_session,
    load_profile_visible,
    load_visible,
    mint_session,
    now_utc,
    plan_for,
    profile_from_fields,
    visible_profiles,
    visible_sessions,
)

_log = logging.getLogger(__name__)

bp = Blueprint("sshim", __name__, template_folder="templates")

# Mint-page defaults (topology A, Windows target ssh.exe, Linux operator ssh, 22, 30min TTL). There is
# NO rendezvous default: the host comes from the admin setting via `default_relay_host()`, which ships
# empty, so the page opens with a blank field the operator must fill with their own box.
DEFAULT_TTL_MINUTES = 30
DEFAULT_ANCHOR_RENDERER = "ssh_exe"   # TARGET/anchor leg = Windows
DEFAULT_TRAVELER_RENDERER = "posix"   # OPERATOR/traveler leg = Linux
#: sshd's own port — what the rendezvous/anchor form fields fall back to when the operator leaves them
#: blank. Not a rendezvous *default*: it is the standard sshd port, and :func:`plan.deviation_risks`
#: flags it as a pinned well-known port when it lands on a public host.
DEFAULT_SSH_PORT = 22


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "sshim_base": cfg.base_template,
        "sshim_version": __version__,
        "sshim_is_admin": current_actor_is_admin(),
        "sshim_topologies": list(TOPOLOGIES),
        "sshim_renderers": list(RENDERERS),
        "sshim_encodings": list(ENCODINGS),
    }


def _split_host_port(value: str) -> tuple[str, int]:
    """`host:port` -> (host, port); bare host -> (host, 22); blank -> ("", 22).

    Best-effort for the admin `default_relay_host` setting, which ships blank."""
    if value and value.count(":") == 1:
        host, _, port = value.partition(":")
        try:
            return host, int(port)
        except ValueError:
            return value, DEFAULT_SSH_PORT
    return value, DEFAULT_SSH_PORT


def _actor_kw() -> dict:
    """The (owner_id, is_admin) authz pair every service call takes — resolved once, one place."""
    return {"owner_id": current_actor_id(), "is_admin": current_actor_is_admin()}


def _load_session_or_404(db, session_id: uuid.UUID):
    row = load_visible(db, session_id, **_actor_kw())
    if row is None:
        abort(404)
    return row


# ── running pivots list ───────────────────────────────────────────────────────

@bp.get("/")
def index():
    """The running-pivots list: active + recently-died sessions (so a TTL death is visible)."""
    now = now_utc()
    with get_config().session_factory() as db:
        rows = visible_sessions(db, **_actor_kw(), now=now)
        sessions = [
            {"row": r, "status": effective_status(r.status, r.expires_at, now=now)} for r in rows
        ]
        return render_template("sshim/sessions.html", sessions=sessions, now=now)


# ── mint ───────────────────────────────────────────────────────────────────────

def _mint_defaults(form=None) -> dict:
    """The mint form's field values: the shipped defaults on a GET, the operator's OWN submitted values on
    a rejected POST (a validation error must never clear what they typed — and the risk banner has to see
    the same values the operator is looking at).

    No rendezvous host here by design: `default_relay_host()` ships empty, so the field starts blank."""
    if form is None:
        host, port = _split_host_port(default_relay_host())
        return {
            "topology": "relay_reverse_socks",
            "rendezvous_host": host,
            "rendezvous_port": port,
            "rendezvous_user": "sshim",
            "name": "",
            "anchor_host": "",
            "anchor_port": DEFAULT_SSH_PORT,
            "anchor_user": "sshim",
            "cidr": "",
            "ttl_minutes": DEFAULT_TTL_MINUTES,
            "anchor_renderer": DEFAULT_ANCHOR_RENDERER,
            "traveler_renderer": DEFAULT_TRAVELER_RENDERER,
        }
    return {
        "topology": (form.get("topology") or "").strip() or "relay_reverse_socks",
        "rendezvous_host": (form.get("rendezvous_host") or "").strip(),
        "rendezvous_port": _int(form.get("rendezvous_port"), DEFAULT_SSH_PORT),
        "rendezvous_user": (form.get("rendezvous_user") or "").strip(),
        "name": (form.get("name") or "").strip(),
        "anchor_host": (form.get("anchor_host") or "").strip(),
        "anchor_port": _int(form.get("anchor_port"), DEFAULT_SSH_PORT),
        "anchor_user": (form.get("anchor_user") or "").strip(),
        "cidr": (form.get("cidr") or "").strip(),
        "ttl_minutes": _int(form.get("ttl_minutes"), DEFAULT_TTL_MINUTES),
        "anchor_renderer": DEFAULT_ANCHOR_RENDERER,
        "traveler_renderer": DEFAULT_TRAVELER_RENDERER,
    }


def _candidate_profile(defaults: dict) -> Profile:
    """An UNVALIDATED ``plan.Profile`` for the mint page's risk banner — only the fields the predicate
    reads are meaningful, and a half-filled or wrong form must still render instead of raising."""
    return Profile(
        name=defaults.get("name") or "",
        topology=defaults.get("topology") or "relay_reverse_socks",
        rendezvous_host=defaults.get("rendezvous_host") or None,
        rendezvous_port=_int(defaults.get("rendezvous_port"), DEFAULT_SSH_PORT),
        anchor_host=defaults.get("anchor_host") or None,
        anchor_port=_int(defaults.get("anchor_port"), DEFAULT_SSH_PORT),
    )


def _render_mint(db, error: str | None = None, *, defaults: dict | None = None):
    """Render the mint form. The risk banner comes from the SAME predicate the detail page uses
    (:func:`plan.deviation_risks`), evaluated against the values currently in the form — silent for the
    shipped blank rendezvous, and loud the moment a pinned well-known port is on screen."""
    defaults = defaults or _mint_defaults()
    return render_template(
        "sshim/mint.html",
        profiles=visible_profiles(db, **_actor_kw()),
        defaults=defaults,
        error=error,
        risk_notes=deviation_risks(_candidate_profile(defaults)),
        max_ttl=max_ttl_minutes(),
    )


@bp.get("/mint")
def mint_form():
    """The command generator's entry form — blank rendezvous, saved profiles listed."""
    with get_config().session_factory() as db:
        return _render_mint(db)


@bp.post("/mint")
def mint():
    """Mint a pivot and render its detail page WITH the one-time secret (shown once, never stored)."""
    form = request.form
    profile_id = None
    saved_id = (form.get("profile_id") or "").strip()
    with get_config().session_factory() as db:
        # A saved profile pre-fills the generator; its fields are the source unless the operator
        # overrode them on the form (an ad-hoc mint). Either way we build + validate a plan.Profile.
        if saved_id:
            try:
                pid = uuid.UUID(saved_id)
            except ValueError:
                abort(400, "bad profile id")
            saved = load_profile_visible(db, pid, **_actor_kw())
            if saved is None:
                abort(404)
            profile_id = saved.id
        try:
            prof = profile_from_fields(
                name=form.get("name") or form.get("rendezvous_host") or "pivot",
                topology=form.get("topology"),
                rendezvous_host=form.get("rendezvous_host"),
                rendezvous_port=form.get("rendezvous_port") or DEFAULT_SSH_PORT,
                rendezvous_user=form.get("rendezvous_user") or "sshim",
                anchor_host=form.get("anchor_host"),
                anchor_port=form.get("anchor_port") or DEFAULT_SSH_PORT,
                anchor_user=form.get("anchor_user") or "sshim",
                cidr=form.get("cidr") or "",
            )
            # Phase 2: when the host provides exclusive_container_acquire, request a core-hosted single-use
            # relay here instead of emitting a command to an operator's own box (lotek#1747; the accessor
            # is deps.exclusive_container_acquire, and Phase 1 deliberately never calls it).
            row, secret = mint_session(
                db, prof,
                owner_id=current_actor_id(), standalone=is_standalone(),
                ttl_minutes=_int(form.get("ttl_minutes"), DEFAULT_TTL_MINUTES),
                max_ttl=max_ttl_minutes(),
                profile_id=profile_id,
                host_audit=host_audit(),
            )
        except Invalid as exc:
            # Re-render the form with the operator's own values and the reason, instead of a bare 400
            # page: a blank rendezvous must read as "configure a rendezvous host", not as a stack trace.
            return _render_mint(db, str(exc), defaults=_mint_defaults(form)), 400
        except Denied as exc:
            abort(403, str(exc))
        return _render_detail(db, row, one_time_secret=secret), 201


@bp.get("/sessions/<uuid:session_id>")
def session_detail(session_id: uuid.UUID):
    """Re-fetch surface: regenerate BOTH legs' full plan from the stored session. No secret (not stored)."""
    with get_config().session_factory() as db:
        row = _load_session_or_404(db, session_id)
        return _render_detail(db, row, one_time_secret=None)


def _render_detail(db, row, *, one_time_secret):
    now = now_utc()
    rendered = plan_for(row)
    return render_template(
        "sshim/detail.html",
        row=row,
        plan=rendered,
        status=effective_status(row.status, row.expires_at, now=now),
        now=now,
        one_time_secret=one_time_secret,
        # Same predicate as the mint page, judged against the session's STORED profile snapshot. Phase 1
        # mints only the ephemeral one-use password, so the credential arm is silent by construction.
        risk_notes=deviation_risks(rendered.session.profile),
        anchor_renderer=DEFAULT_ANCHOR_RENDERER,
        traveler_renderer=DEFAULT_TRAVELER_RENDERER,
    )


@bp.post("/sessions/<uuid:session_id>/kill")
def kill(session_id: uuid.UUID):
    with get_config().session_factory() as db:
        row = _load_session_or_404(db, session_id)
        try:
            kill_session(db, row, **_actor_kw(), host_audit=host_audit())
        except Denied as exc:
            abort(403, str(exc))
    return redirect(url_for("sshim.index"))


# ── profiles ───────────────────────────────────────────────────────────────────

@bp.get("/profiles")
def profiles():
    q = (request.args.get("q") or "").strip() or None
    with get_config().session_factory() as db:
        rows = visible_profiles(db, **_actor_kw(), q=q)
        return render_template("sshim/profiles.html", profiles=rows, q=q or "")


@bp.post("/profiles")
def create_profile_route():
    form = request.form
    with get_config().session_factory() as db:
        try:
            prof = profile_from_fields(
                name=form.get("name"),
                topology=form.get("topology"),
                rendezvous_host=form.get("rendezvous_host"),
                rendezvous_port=form.get("rendezvous_port") or DEFAULT_SSH_PORT,
                rendezvous_user=form.get("rendezvous_user") or "sshim",
                anchor_host=form.get("anchor_host"),
                anchor_port=form.get("anchor_port") or DEFAULT_SSH_PORT,
                anchor_user=form.get("anchor_user") or "sshim",
                cidr=form.get("cidr") or "",
            )
            create_profile(db, prof, owner_id=current_actor_id(), standalone=is_standalone())
        except Invalid as exc:
            abort(400, str(exc))
        except Denied as exc:
            abort(403, str(exc))
    return redirect(url_for("sshim.profiles"))


@bp.post("/profiles/<uuid:profile_id>/delete")
def delete_profile_route(profile_id: uuid.UUID):
    with get_config().session_factory() as db:
        row = load_profile_visible(db, profile_id, **_actor_kw())
        if row is None:
            abort(404)
        try:
            delete_profile(db, row, **_actor_kw())
        except Denied as exc:
            abort(403, str(exc))
    return redirect(url_for("sshim.profiles"))


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
