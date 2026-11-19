"""Human-facing UI — the running-pivots list, the mint page, and the re-fetchable session detail.

Every route resolves authorization + liveness through ``sshim.service`` / ``sshim.plan`` — the SAME
functions the PAT machine API uses, so the rules cannot drift between the two surfaces. A session/profile
the caller may not see is **404, never 403** (INV-TENANCY-01, no existence oracle).

The pivot's SSH identity (user + password) is shown on the mint RESPONSE page, and again on the session
DETAIL page for its owner/admin — the password is stored sealed (``sshim.secretbox``) precisely so the
borrowed-box re-fetch works. Neither page puts it in a cookie, and a killed session refuses it.

There is no core reaper in Phase 1, so :func:`_sweep_hook` runs a throttled ``before_request`` sweep
(``service.SWEEP_INTERVAL_SECONDS``) that notices sshim's own expiries on any page load.
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
    exclusive_container_acquire,
    exclusive_container_release,
    get_config,
    host_audit,
    is_standalone,
    max_ttl_minutes,
    relay_connected,
    secret_key_path,
)
from sshim.plan import (
    ENCODINGS,
    RENDERERS,
    TOPOLOGIES,
    Profile,
    deviation_risks,
    effective_status,
    status_rank,
)
from sshim.service import (
    Denied,
    Invalid,
    SessionDead,
    create_profile,
    delete_profile,
    give_back_relay,
    kill_many,
    kill_session,
    load_profile_visible,
    load_visible,
    maybe_sweep,
    mint_session,
    now_utc,
    parse_kill_at,
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
DEFAULT_OPERATOR_RENDERER = "posix"   # OPERATOR/operator leg = Linux
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


@bp.before_request
def _sweep_hook():
    """Notice sshim's OWN expiries on any page load: there is no core reaper in Phase 1.

    Throttled to once per ``service.SWEEP_INTERVAL_SECONDS`` per process (see ``service.maybe_sweep``),
    so a burst of requests costs one UPDATE rather than one per request. A failure here must never break
    the page it decorates — the sweep is housekeeping, and the liveness predicate does not depend on it
    (``kill_at``/TTL are still derived correctly without a single sweep; the sweep only persists the
    terminal status)."""
    try:
        with get_config().session_factory() as db:
            maybe_sweep(db)
    except Exception:  # noqa: BLE001 - housekeeping must not 500 a page
        _log.warning("sshim expiry sweep failed", exc_info=True)


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
        sessions = []
        for r in rows:
            status = effective_status(r.status, r.expires_at, now=now, kill_at=r.kill_at)
            sessions.append({
                "row": r,
                "status": status,
                # Presentation-only lifecycle order (active -> expired -> killed); the ONE predicate is
                # plan.status_rank so a second copy of this ordering cannot appear in a template.
                "status_rank": status_rank(status),
                "rendezvous_port": (r.params or {}).get("rendezvous_port"),
            })
        return render_template("sshim/sessions.html", sessions=sessions, now=now)


# ── mint ───────────────────────────────────────────────────────────────────────

#: The built-in PRIMARY profile. Not a saved row: it resolves to THIS dashboard's own host, so it is
#: correct on every deployment without a hardcoded name. The lotek box already runs sshd, which makes it
#: the zero-setup rendezvous for an operator who has an account there.
PRIMARY_PROFILE_ID = "__lotek_host__"
PRIMARY_PROFILE_LABEL = "lotek host (this dashboard)"
PRIMARY_RELAY_PORT = 22


def primary_relay() -> tuple[str, int]:
    """``(host, port)`` of the lotek host acting as the rendezvous.

    The admin ``default_relay_host`` setting wins when an operator pinned one (a smaller public face, a
    different sshd); otherwise the host the request arrived on IS the rendezvous. Port 22 is the
    dashboard host's own sshd, and `deviation_risks` flags it as a well-known port on every surface —
    the convenience is loud, never silent.
    """
    configured, cport = _split_host_port(default_relay_host())
    host = configured or (request.host or "").split(":")[0]
    return host, (cport or PRIMARY_RELAY_PORT)


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
            "rendezvous_user": "",   # "" = generate a fresh identity per pivot
            "name": "",
            "anchor_host": "",
            "anchor_port": DEFAULT_SSH_PORT,
            "anchor_user": "",
            "cidr": "",
            "ttl_minutes": DEFAULT_TTL_MINUTES,
            "kill_at": "",
            "anchor_renderer": DEFAULT_ANCHOR_RENDERER,
            "operator_renderer": DEFAULT_OPERATOR_RENDERER,
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
        "kill_at": (form.get("kill_at") or "").strip(),
        "anchor_renderer": DEFAULT_ANCHOR_RENDERER,
        "operator_renderer": DEFAULT_OPERATOR_RENDERER,
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
    _primary_host, _primary_port = primary_relay()
    return render_template(
        "sshim/mint.html",
        profiles=visible_profiles(db, **_actor_kw()),
        defaults=defaults,
        error=error,
        risk_notes=deviation_risks(_candidate_profile(defaults)),
        max_ttl=max_ttl_minutes(),
        primary_host=_primary_host,
        primary_port=_primary_port,
        primary_label=PRIMARY_PROFILE_LABEL,
        primary_id=PRIMARY_PROFILE_ID,
    )


@bp.get("/mint")
def mint_form():
    """The command generator's entry form — blank rendezvous, saved profiles listed."""
    with get_config().session_factory() as db:
        return _render_mint(db)


@bp.post("/mint")
def mint():
    """Mint a pivot and render its detail page with the identity that was just generated for it."""
    form = request.form
    profile_id = None
    saved_id = (form.get("profile_id") or "").strip()
    with get_config().session_factory() as db:
        # A saved profile pre-fills the generator; its fields are the source unless the operator
        # overrode them on the form (an ad-hoc mint). Either way we build + validate a plan.Profile.
        if saved_id == PRIMARY_PROFILE_ID:
            # The PRIMARY profile is not a row: it means "rendezvous on this lotek host". Resolve it
            # server-side so a submit works even with JS off, and so the host is whatever this
            # deployment actually answers on rather than a name shipped in the package.
            _ph, _pp = primary_relay()
            form = {**form, "rendezvous_host": _ph, "rendezvous_port": str(_pp)}
        elif saved_id:
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
                rendezvous_user=form.get("rendezvous_user"),
                anchor_host=form.get("anchor_host"),
                anchor_port=form.get("anchor_port") or DEFAULT_SSH_PORT,
                anchor_user=form.get("anchor_user"),
                cidr=form.get("cidr") or "",
            )
            # Phase 2: when the host provides exclusive_container_acquire, request a core-hosted single-use
            # relay here instead of emitting a command to an operator's own box (lotek#1747; the accessor
            # is deps.exclusive_container_acquire, and Phase 1 deliberately never calls it).
            row, _ssh_password = mint_session(
                db, prof,
                owner_id=current_actor_id(), standalone=is_standalone(),
                ttl_minutes=_int(form.get("ttl_minutes"), DEFAULT_TTL_MINUTES),
                max_ttl=max_ttl_minutes(),
                secret_key_path=secret_key_path(),
                profile_id=profile_id,
                kill_at=parse_kill_at(form.get("kill_at")),
                host_audit=host_audit(),
                # Phase 2: when the host exposes exclusive_container_acquire, the session rides a
                # core-hosted single-use relay that PROVISIONS this pivot's random identity. Absent
                # (standalone, or the seam not yet deployed) it is None and behaviour is Phase 1's.
                acquire=exclusive_container_acquire(),
            )
        except Invalid as exc:
            # Re-render the form with the operator's own values and the reason, instead of a bare 400
            # page: a blank rendezvous must read as "configure a rendezvous host", not as a stack trace.
            return _render_mint(db, str(exc), defaults=_mint_defaults(form)), 400
        except Denied as exc:
            abort(403, str(exc))
        return _render_detail(db, row), 201


@bp.get("/sessions/<uuid:session_id>")
def session_detail(session_id: uuid.UUID):
    """Re-fetch surface: regenerate BOTH legs' full plan, plus this pivot's own identity.

    The password is unsealed for the owner/admin here (``service.plan_for`` -> ``reveal_password``), which
    is the whole reason the row stores it sealed; a killed session renders its death instead."""
    with get_config().session_factory() as db:
        row = _load_session_or_404(db, session_id)
        return _render_detail(db, row)


def _render_detail(db, row):
    now = now_utc()
    status = effective_status(row.status, row.expires_at, now=now, kill_at=row.kill_at)
    try:
        rendered = plan_for(row, secret_key_path=secret_key_path(), now=now)
    except SessionDead as exc:
        # A killed pivot (stored `killed`, or a reached `kill_at`) is torn down: no commands, no secret.
        # The row stays viewable so the death is VISIBLE rather than a mystery 404.
        return render_template(
            "sshim/detail.html",
            row=row,
            plan=None,
            status=status,
            now=now,
            dead_reason=str(exc),
            risk_notes=(),
            anchor_renderer=DEFAULT_ANCHOR_RENDERER,
            operator_renderer=DEFAULT_OPERATOR_RENDERER,
        )
    return render_template(
        "sshim/detail.html",
        row=row,
        plan=rendered,
        status=status,
        now=now,
        dead_reason=None,
        # Same predicate as the mint page, judged against the session's STORED profile snapshot.
        risk_notes=deviation_risks(rendered.session.profile),
        anchor_renderer=DEFAULT_ANCHOR_RENDERER,
        operator_renderer=DEFAULT_OPERATOR_RENDERER,
    )


@bp.post("/sessions/<uuid:session_id>/kill")
def kill(session_id: uuid.UUID):
    with get_config().session_factory() as db:
        row = _load_session_or_404(db, session_id)
        # A kill is one-way: the operator must re-stage the anchor side and re-dial. When a leg is
        # still connected that is a surprising thing to do by a single click, so ask first. The
        # confirmation swears to nothing it cannot see (Phase 1 cannot observe the rendezvous).
        if (request.form.get("confirm") or "") != "1":
            return render_template(
                "sshim/confirm_kill.html",
                row=row,
                connected=relay_connected(row),
                status=effective_status(
                    row.status, row.expires_at, now=now_utc(), kill_at=row.kill_at
                ),
            )
        try:
            kill_session(db, row, **_actor_kw(), host_audit=host_audit())
        except Denied as exc:
            abort(403, str(exc))
        # Give the container back. A host that already reaped it (TTL, or an admin reap) must not make
        # the kill fail — the operator asked to tear this pivot down, and it IS down either way.
        give_back_relay(row, exclusive_container_release())
    return redirect(url_for("sshim.index"))


# ── bulk kill ─────────────────────────────────────────────────────────────────

def _bulk_ids() -> list[str]:
    """The requested session ids: repeated `ids` fields, and a comma-separated form as a convenience.

    Kept as raw strings on purpose — :func:`service.kill_many` owns the uuid parsing so a malformed id
    is reported as `not_found` exactly like an unknown one, instead of a 400 that would tell a caller
    which shape exists."""
    ids: list[str] = []
    for value in request.form.getlist("ids"):
        ids.extend(part.strip() for part in value.split(",") if part.strip())
    return ids


@bp.post("/sessions/bulk-kill")
def bulk_kill():
    """Kill the selected pivots, with a MANDATORY confirmation that lists every target.

    The confirmation is server-rendered (mirroring the single-pivot kill's ``confirm=1`` gate), so it
    works with JS off and cannot be skipped by a client that never loaded the page script. It lists
    profile + rendezvous + port for each visible target; ids the caller may not see are carried
    through to the result as `not_found` rather than being hidden from the report.
    """
    ids = _bulk_ids()
    with get_config().session_factory() as db:
        if (request.form.get("confirm") or "") != "1":
            rows = []
            for raw in ids:
                try:
                    session_id = uuid.UUID(raw)
                except (TypeError, ValueError):
                    continue
                row = load_visible(db, session_id, **_actor_kw())
                if row is not None:
                    rows.append(row)
            return render_template("sshim/confirm_bulk_kill.html", rows=rows, ids=ids)
        results = kill_many(
            db, ids, **_actor_kw(), host_audit=host_audit(), release=exclusive_container_release()
        )
    return render_template("sshim/bulk_kill_result.html", results=results)


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
                rendezvous_user=form.get("rendezvous_user"),
                anchor_host=form.get("anchor_host"),
                anchor_port=form.get("anchor_port") or DEFAULT_SSH_PORT,
                anchor_user=form.get("anchor_user"),
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
