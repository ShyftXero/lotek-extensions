"""sshim command-plan generator — the revshells-style core of Phase 1.

Pure functions: a Profile + a session's random ports in, four renderings of two
legs out. No Flask, no DB, no docker, no lotek/`app.*` import. This is the piece
that ships the use case with zero core changes. Folded verbatim (behaviour-wise)
from the proven `sshim_gen.py` (11/11 tested), plus two Phase-1 additions:

* :func:`encode_command` — the revshells-style post-transform (None/URL/URL-double/Base64).
* :func:`effective_status` / :func:`is_listable` — the ONE liveness predicate every
  surface (list page, detail view, machine API) calls. `expired` is DERIVED from
  `expires_at`, never stored: no sweeper, no second copy to drift.

Topologies
----------
A. relay_reverse_socks  — both sides behind NAT, rendezvous is a 3rd-party box.
   anchor:  ssh -N -R 127.0.0.1:<socks> <ru>@<rh> -p <rport>
   traveler: ssh -N -L <lsocks>:127.0.0.1:<socks> <ru>@<rh> -p <rport>
   Remote DNS is FREE: the anchor's ssh client resolves names inside the target
   network (bare -R = remote dynamic forward; OpenSSH >= 7.6).

B. direct_socks          — anchor has a routable address (public IP / tailnet).
   traveler: ssh -N -D <lsocks> <au>@<anchor> -p <aport>
   OpenSSH resolves LOCALLY here, so this is NOT socks5h remote DNS. Stated, not
   hidden.

C. relay_sshuttle        — Linux traveler wants full-subnet L3, not per-app SOCKS.
   anchor:  ssh -N -R 127.0.0.1:<jump>:127.0.0.1:22 <ru>@<rh> -p <rport>
   traveler: sshuttle --dns -e 'ssh -J <ru>@<rh>:<rport>' -r <au>@127.0.0.1:<jump> <cidr>
   Anchor needs Python 3; traveler needs sudo. Documented.

Port model
----------
No reservation. sshim picks a random loopback port on the rendezvous per session;
both legs use the same number. If two sessions collide on the same rendezvous, the
second `ssh -R` fails immediately under ExitOnForwardFailure=yes and sshim
regenerates. There is no TOCTOU because there is no allocator to race against.

Residue honesty
---------------
`ssh` writes a known_hosts line. Every emitted command carries
`-o UserKnownHostsFile=/dev/null` (POSIX) or the NUL equivalent (Windows) and
`-o StrictHostKeyChecking=accept-new -o LogLevel=ERROR`. Shell history on a
borrowed box is the one residue left; the UI says so, does not pretend otherwise.
The rendezvous password is NEVER placed on argv (visible in `ps`); the operator
pastes it at the prompt. Because it is never on argv, a stored session can be
RE-RENDERED to byte-identical commands without the secret — which is exactly what
makes Phase-1 re-fetch (the running-pivots list) correct and secret-free.
"""

from __future__ import annotations

import base64
import secrets
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# ── data ─────────────────────────────────────────────────────────────────────

TOPOLOGIES = ("relay_reverse_socks", "direct_socks", "relay_sshuttle")
RENDERERS = ("posix", "ssh_exe", "powershell_encoded", "sshuttle")
ENCODINGS = ("none", "url", "url_double", "base64")
_PORT_MIN, _PORT_MAX = 1024, 65535


@dataclass(frozen=True)
class Profile:
    """A named target. Switching targets is a dropdown, not a re-typed command."""
    name: str
    topology: str
    # rendezvous (topology A/C) — the always-on box both legs dial.
    rendezvous_host: str | None = None
    rendezvous_port: int = 22
    rendezvous_user: str = "sshim"
    # anchor (topology B; also the sshd box for topology C).
    anchor_host: str | None = None
    anchor_port: int = 22
    anchor_user: str = "sshim"
    # subnet to route (topology C only). "" = let sshuttle -N auto-detect.
    cidr: str = ""


@dataclass(frozen=True)
class Session:
    """One minted shim: the random ports + creds for a single use."""
    profile: Profile
    socks_port: int            # loopback on rendezvous (A) / traveler (B)
    jump_port: int             # loopback on rendezvous exposing anchor sshd (C)
    rendezvous_password: str   # pasted at prompt, never on argv
    ttl_minutes: int = 30


def random_port() -> int:
    """A random TCP port in the ephemeral range. Not reserved — see module docstring."""
    return secrets.randbelow(_PORT_MAX - _PORT_MIN) + _PORT_MIN


def mint(profile: Profile, *, ttl_minutes: int = 30) -> Session:
    """Pick the session's random ports + a throwaway password. No side effects."""
    return Session(
        profile=profile,
        socks_port=random_port(),
        jump_port=random_port(),
        rendezvous_password=gen_password(),
        ttl_minutes=ttl_minutes,
    )


def gen_password() -> str:
    """A URL-safe throwaway secret, ~26 chars of entropy. Pasted at the prompt, never on argv.

    Public (not `_`-prefixed) because the mint surface shows it ONCE and then forgets it —
    sshim never persists the cleartext (see :func:`rebuild_session`)."""
    return secrets.token_urlsafe(18)


# ── shared ssh option strings ────────────────────────────────────────────────

# Disposable: no known_hosts, accept-new host key, quiet. Loopback-only forwards.
_POSIX_OPTS = (
    "-o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=accept-new "
    "-o LogLevel=ERROR -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 "
    "-o ServerAliveCountMax=3 -N"
)
# Windows ssh.exe: NUL device for known hosts. Same options, NUL spellings.
_WIN_OPTS = (
    "-o UserKnownHostsFile=NUL -o StrictHostKeyChecking=accept-new "
    "-o LogLevel=ERROR -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 "
    "-o ServerAliveCountMax=3 -N"
)


def _validate(profile: Profile) -> None:
    if profile.topology not in TOPOLOGIES:
        raise ValueError(f"topology must be one of {TOPOLOGIES}, got {profile.topology!r}")
    if profile.topology in ("relay_reverse_socks", "relay_sshuttle") and not profile.rendezvous_host:
        raise ValueError(f"{profile.topology} requires rendezvous_host")
    if profile.topology == "direct_socks" and not profile.anchor_host:
        raise ValueError("direct_socks requires anchor_host")
    if profile.topology == "relay_sshuttle" and not profile.anchor_host:
        raise ValueError("relay_sshuttle requires anchor_host (the sshd box sshuttle reaches)")


# ── topology A: relay + reverse SOCKS5 (remote DNS free) ─────────────────────

def _anchor_cmd_a(sess: Session, renderer: str) -> str:
    p = sess.profile
    target = f"{p.rendezvous_user}@{p.rendezvous_host} -p {p.rendezvous_port}"
    fwd = f"-R 127.0.0.1:{sess.socks_port}"
    if renderer == "posix":
        return f"ssh {fwd} {_POSIX_OPTS} -- {target}"
    if renderer == "ssh_exe":
        return f"ssh.exe {fwd} {_WIN_OPTS} -- {target}"
    if renderer == "powershell_encoded":
        return _powershell_encoded(f"ssh.exe {fwd} {_WIN_OPTS} -- {target}")
    raise ValueError(f"topology A anchor has no {renderer!r} rendering; use posix/ssh_exe/powershell_encoded")


def _traveler_cmd_a(sess: Session, renderer: str) -> str:
    p = sess.profile
    local = sess.socks_port  # traveler reuses the same loopback number for clarity
    target = f"{p.rendezvous_user}@{p.rendezvous_host} -p {p.rendezvous_port}"
    fwd = f"-L {local}:127.0.0.1:{sess.socks_port}"
    hint = (
        f"  # then point your browser/CLI at socks5h://127.0.0.1:{local} "
        f"(remote DNS — the anchor resolves names)"
    )
    if renderer == "posix":
        return f"ssh {fwd} {_POSIX_OPTS} -- {target}{hint}"
    if renderer == "ssh_exe":
        return f"ssh.exe {fwd} {_WIN_OPTS} -- {target}{hint}"
    if renderer == "powershell_encoded":
        return _powershell_encoded(f"ssh.exe {fwd} {_WIN_OPTS} -- {target}") + hint
    raise ValueError(f"topology A traveler has no {renderer!r} rendering")


# ── topology B: direct SOCKS (anchor routable) ──────────────────────────────

def _traveler_cmd_b(sess: Session, renderer: str) -> str:
    p = sess.profile
    target = f"{p.anchor_user}@{p.anchor_host} -p {p.anchor_port}"
    fwd = f"-D {sess.socks_port}"
    # NOTE: -D resolves on the TRAVELER, not the anchor. Not socks5h. Stated.
    hint = (
        f"  # socks5://127.0.0.1:{sess.socks_port} — NOTE: OpenSSH -D resolves DNS "
        f"locally (traveler), so this is NOT remote DNS. Use topology A for that."
    )
    if renderer == "posix":
        return f"ssh {fwd} {_POSIX_OPTS} -- {target}{hint}"
    if renderer == "ssh_exe":
        return f"ssh.exe {fwd} {_WIN_OPTS} -- {target}{hint}"
    if renderer == "powershell_encoded":
        return _powershell_encoded(f"ssh.exe {fwd} {_WIN_OPTS} -- {target}") + hint
    raise ValueError(f"traveler has no {renderer!r} rendering; use posix/ssh_exe/powershell_encoded")


# ── topology C: relay + sshuttle (full-subnet L3, Linux traveler) ────────────

def _anchor_cmd_c(sess: Session, renderer: str) -> str:
    p = sess.profile
    # expose the anchor's OWN sshd on the rendezvous loopback at jump_port
    target = f"{p.rendezvous_user}@{p.rendezvous_host} -p {p.rendezvous_port}"
    fwd = f"-R 127.0.0.1:{sess.jump_port}:127.0.0.1:22"
    if renderer == "posix":
        return f"ssh {fwd} {_POSIX_OPTS} -- {target}"
    if renderer == "ssh_exe":
        return f"ssh.exe {fwd} {_WIN_OPTS} -- {target}"
    if renderer == "powershell_encoded":
        return _powershell_encoded(f"ssh.exe {fwd} {_WIN_OPTS} -- {target}")
    raise ValueError(f"topology C anchor has no {renderer!r} rendering")


def _traveler_cmd_c(sess: Session, renderer: str) -> str:
    p = sess.profile
    if renderer != "sshuttle":
        raise ValueError("topology C traveler is sshuttle-only (needs sudo + Python on anchor)")
    cidr = p.cidr or "-N"  # -N = auto-detect all private subnets on the anchor
    jump = f"{p.rendezvous_user}@{p.rendezvous_host}:{p.rendezvous_port}"
    remote = f"{p.anchor_user}@127.0.0.1:{sess.jump_port}"
    # -x the hotel gateway if the hotel LAN overlaps the home subnet (operator fills it).
    hint = (
        f"  # add -x <hotel_gateway>/32 if the hotel LAN overlaps {p.cidr or 'your home subnet'}; "
        f"needs sudo on the traveler + Python on the anchor"
    )
    return f"sshuttle --dns -e 'ssh -J {jump}' -r {remote} {cidr}{hint}"


# ── powershell -EncodedCommand rendering ─────────────────────────────────────

def _powershell_encoded(cmd: str) -> str:
    """Wrap a cmd one-liner as `powershell -EncodedCommand <base64>`.

    Keeps the command out of the borrow box's shell history (powershell.exe logs
    the encoded form, not the cleartext). UTF-16LE + base64, per Microsoft's spec.
    """
    blob = base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")
    return "powershell -NoProfile -EncodedCommand " + blob


# ── encoding post-transform (revshells-style) ────────────────────────────────

def encode_command(cmd: str, encoding: str) -> str:
    """Apply an optional transport encoding to a rendered command (None/URL/URL-double/Base64).

    Independent of the renderer: a command is built once, then optionally wrapped for a transport
    that mangles spaces/quotes (a chat box, a URL param, a QR). `none` is identity. Base64 here is
    plain UTF-8 b64 of the whole string — distinct from the `powershell_encoded` RENDERER, which is
    UTF-16LE for powershell's own `-EncodedCommand`."""
    if encoding == "none":
        return cmd
    if encoding == "url":
        return urllib.parse.quote(cmd, safe="")
    if encoding == "url_double":
        return urllib.parse.quote(urllib.parse.quote(cmd, safe=""), safe="")
    if encoding == "base64":
        return base64.b64encode(cmd.encode("utf-8")).decode("ascii")
    raise ValueError(f"encoding must be one of {ENCODINGS}, got {encoding!r}")


# ── the plan ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Plan:
    """A rendered session. Each leg carries its own map of renderer -> command,
    because topology C's anchor (ssh) and traveler (sshuttle) use different
    renderer families.
    """
    session: Session
    anchor: dict[str, str] = field(default_factory=dict)     # renderer -> command
    traveler: dict[str, str] = field(default_factory=dict)   # renderer -> command
    notes: tuple[str, ...] = ()


# The renderers that apply to a normal ssh leg (every topology's anchor, and
# the traveler legs of topologies A and B). Topology C's traveler is sshuttle-only.
_SSH_RENDERERS = ("posix", "ssh_exe", "powershell_encoded")


def render_plan(sess: Session) -> Plan:
    """Build every applicable rendering of both legs for the session's topology.

    The password is deliberately NOT in any command — the operator pastes it at
    the ssh prompt. A command with the password on argv would leak it to every
    `ps` viewer on the borrowed box. (This is also why a stored session re-renders
    without the secret: see :func:`rebuild_session`.)
    """
    _validate(sess.profile)
    t = sess.profile.topology
    anchor: dict[str, str] = {}
    traveler: dict[str, str] = {}
    notes: list[str] = []

    if t == "relay_reverse_socks":
        for r in _SSH_RENDERERS:
            anchor[r] = _anchor_cmd_a(sess, r)
            traveler[r] = _traveler_cmd_a(sess, r)
        notes.append("Remote DNS is free: the anchor resolves names inside the target network.")
    elif t == "direct_socks":
        for r in _SSH_RENDERERS:
            traveler[r] = _traveler_cmd_b(sess, r)
        notes.append("Direct -D resolves DNS on the TRAVELER — not remote DNS. Use relay mode for that.")
    elif t == "relay_sshuttle":
        for r in _SSH_RENDERERS:
            anchor[r] = _anchor_cmd_c(sess, r)
        traveler["sshuttle"] = _traveler_cmd_c(sess, "sshuttle")
        notes.append("sshuttle needs sudo on the traveler and Python 3 on the anchor.")

    notes.append("Password is pasted at the ssh prompt, never on the command line.")
    notes.append("Both legs die on TTL or disconnect; nothing persists but shell history.")
    return Plan(session=sess, anchor=anchor, traveler=traveler, notes=tuple(notes))


# ── stored-session reconstruction (Phase 1 re-fetch) ─────────────────────────

def rebuild_session(params: dict, *, socks_port: int, jump_port: int, ttl_minutes: int) -> Session:
    """Rebuild a :class:`Session` from a stored profile-snapshot + the stored ports.

    `params` is the JSON snapshot of the minting :class:`Profile` (``Profile`` field names).
    The password is intentionally ``""``: sshim never persists the cleartext, and
    :func:`render_plan` never puts it on a command, so the regenerated plan is byte-identical
    to the one shown at mint. The caller (the UI/machine surface) states that the secret can't
    be re-shown and offers a re-mint — it never shows a stale or empty secret as if it were real.
    """
    profile = Profile(**params)
    return Session(
        profile=profile,
        socks_port=socks_port,
        jump_port=jump_port,
        rendezvous_password="",
        ttl_minutes=ttl_minutes,
    )


def profile_params(profile: Profile) -> dict:
    """The JSON-serialisable snapshot of a Profile to store on a session row."""
    return {
        "name": profile.name,
        "topology": profile.topology,
        "rendezvous_host": profile.rendezvous_host,
        "rendezvous_port": profile.rendezvous_port,
        "rendezvous_user": profile.rendezvous_user,
        "anchor_host": profile.anchor_host,
        "anchor_port": profile.anchor_port,
        "anchor_user": profile.anchor_user,
        "cidr": profile.cidr,
    }


# ── liveness predicate — ONE home, every surface calls it ────────────────────

#: A died session (expired or killed) stays on the list this long so a TTL death is VISIBLE
#: (a disappeared pivot would look like a mystery 404). After this, it ages off the list.
LISTABLE_GRACE = timedelta(hours=1)

# Stored status values. `expired` is DERIVED from expires_at and is NEVER stored — storing it
# would need a sweeper and would be a second copy of the liveness fact that could drift.
STATUS_ACTIVE = "active"
STATUS_KILLED = "killed"
STATUS_EXPIRED = "expired"  # derived only, returned by effective_status


def effective_status(stored_status: str, expires_at: datetime, *, now: datetime) -> str:
    """The single liveness predicate: killed > expired > active.

    `stored_status` is the explicit operator action (`active`/`killed`). Expiry is computed here
    from `expires_at` vs `now` so there is exactly one place that decides whether a pivot is live.
    """
    if stored_status == STATUS_KILLED:
        return STATUS_KILLED
    if now >= expires_at:
        return STATUS_EXPIRED
    return STATUS_ACTIVE


def is_active(stored_status: str, expires_at: datetime, *, now: datetime) -> bool:
    """True iff the pivot is live right now (commands are expected to work)."""
    return effective_status(stored_status, expires_at, now=now) == STATUS_ACTIVE


def is_listable(expires_at: datetime, *, now: datetime, grace: timedelta = LISTABLE_GRACE) -> bool:
    """Single list-visibility predicate: show active + recently-died sessions (within `grace`)."""
    return now < expires_at + grace
