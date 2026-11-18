"""Pin the sshim command generator — every topology x renderer, encoding, and the liveness predicate.

Folded from the proven `test_sshim_gen.py` (the pure-Python baseline, 11/11) and extended for the two
Phase-1 additions: the `encode_command` transport post-transform and the single liveness predicate
(`effective_status` / `is_listable`) that every surface calls. No Flask, no DB, no `app.*` — strings in,
strings out.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta

import pytest

from sshim.plan import (
    LISTABLE_GRACE,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_KILLED,
    Profile,
    _traveler_cmd_c,
    effective_status,
    encode_command,
    is_active,
    is_listable,
    mint,
    profile_params,
    random_port,
    rebuild_session,
    render_plan,
)


def _profile(topology: str, **kw) -> Profile:
    base = dict(name="t", topology=topology)
    base.update(kw)
    return Profile(**base)


# ── topology A: relay + reverse SOCKS5, remote DNS free ─────────────────────

def test_topology_a_posix_round_trip():
    p = _profile("relay_reverse_socks", rendezvous_host="vps.example", rendezvous_port=443)
    sess = mint(p, ttl_minutes=15)
    plan = render_plan(sess)

    assert "posix" in plan.anchor and "posix" in plan.traveler
    anchor, traveler = plan.anchor["posix"], plan.traveler["posix"]
    assert f"-R 127.0.0.1:{sess.socks_port}" in anchor
    assert "-N" in anchor
    assert "ExitOnForwardFailure=yes" in anchor
    assert "UserKnownHostsFile=/dev/null" in anchor
    assert "sshim@vps.example -p 443" in anchor
    assert f"-L {sess.socks_port}:127.0.0.1:{sess.socks_port}" in traveler
    assert sess.rendezvous_password not in anchor
    assert sess.rendezvous_password not in traveler
    assert any("Remote DNS is free" in n for n in plan.notes)


def test_topology_a_ssh_exe_uses_NUL_known_hosts():
    p = _profile("relay_reverse_socks", rendezvous_host="vps.example")
    plan = render_plan(mint(p))
    assert "UserKnownHostsFile=NUL" in plan.anchor["ssh_exe"]
    assert "ssh.exe" in plan.anchor["ssh_exe"]


def test_topology_a_powershell_encoded_decodes_back():
    p = _profile("relay_reverse_socks", rendezvous_host="vps.example")
    sess = mint(p)
    plan = render_plan(sess)
    cmd = plan.anchor["powershell_encoded"]
    assert cmd.startswith("powershell -NoProfile -EncodedCommand ")
    blob = cmd.split("EncodedCommand ", 1)[1]
    decoded = base64.b64decode(blob).decode("utf-16-le")
    assert f"-R 127.0.0.1:{sess.socks_port}" in decoded
    assert "ssh.exe" in decoded


# ── topology B: direct SOCKS, anchor routable ───────────────────────────────

def test_topology_b_direct_socks_no_anchor_leg():
    p = _profile("direct_socks", anchor_host="home.tail.net", anchor_port=22)
    sess = mint(p)
    plan = render_plan(sess)
    assert plan.anchor == {}
    traveler = plan.traveler["posix"]
    assert f"-D {sess.socks_port}" in traveler
    assert "sshim@home.tail.net -p 22" in traveler
    assert any("not remote DNS" in n for n in plan.notes)
    assert "NOT remote DNS" in traveler


# ── topology C: relay + sshuttle, full-subnet L3 ─────────────────────────────

def test_topology_c_sshuttle_uses_proxyjump():
    p = _profile(
        "relay_sshuttle", rendezvous_host="vps.example", rendezvous_port=443,
        anchor_host="home-pi.local", anchor_user="pi", cidr="192.168.1.0/24",
    )
    sess = mint(p)
    plan = render_plan(sess)
    assert f"-R 127.0.0.1:{sess.jump_port}:127.0.0.1:22" in plan.anchor["posix"]
    travel = plan.traveler["sshuttle"]
    assert "sshuttle --dns" in travel
    assert "-e 'ssh -J sshim@vps.example:443'" in travel
    assert f"-r pi@127.0.0.1:{sess.jump_port}" in travel
    assert "192.168.1.0/24" in travel
    assert any("sudo" in n for n in plan.notes)


def test_topology_c_sshuttle_auto_detects_when_no_cidr():
    p = _profile("relay_sshuttle", rendezvous_host="vps.example", anchor_host="home-pi.local", cidr="")
    plan = render_plan(mint(p))
    assert " -N" in plan.traveler["sshuttle"]


# ── validation ──────────────────────────────────────────────────────────────

def test_relay_topology_without_rendezvous_rejected():
    with pytest.raises(ValueError, match="rendezvous_host"):
        render_plan(mint(_profile("relay_reverse_socks")))


def test_direct_topology_without_anchor_rejected():
    with pytest.raises(ValueError, match="anchor_host"):
        render_plan(mint(_profile("direct_socks")))


def test_topology_c_traveler_rejects_non_sshuttle_renderer():
    sess = mint(_profile("relay_sshuttle", rendezvous_host="v", anchor_host="a"))
    with pytest.raises(ValueError, match="sshuttle-only"):
        _traveler_cmd_c(sess, "posix")


# ── residue + port model ────────────────────────────────────────────────────

def test_random_port_in_ephemeral_range():
    for _ in range(1000):
        assert 1024 <= random_port() <= 65535


def test_password_never_on_any_command_line():
    sess = mint(_profile("relay_reverse_socks", rendezvous_host="vps.example"))
    plan = render_plan(sess)
    for cmd in [*plan.anchor.values(), *plan.traveler.values()]:
        assert sess.rendezvous_password not in cmd


# ── encoding post-transform ──────────────────────────────────────────────────

def test_encode_none_is_identity():
    assert encode_command("ssh -N x@y", "none") == "ssh -N x@y"


def test_encode_url_and_double():
    once = encode_command("a b&c", "url")
    assert once == "a%20b%26c"
    assert encode_command("a b&c", "url_double") == "a%2520b%2526c"


def test_encode_base64_round_trips():
    blob = encode_command("ssh -N sshim@vps", "base64")
    assert base64.b64decode(blob).decode() == "ssh -N sshim@vps"


def test_encode_unknown_rejected():
    with pytest.raises(ValueError, match="encoding must be one of"):
        encode_command("x", "rot13")


# ── re-fetch: rebuild a stored session WITHOUT the secret ────────────────────

def test_rebuild_session_reproduces_commands_without_secret():
    p = _profile("relay_reverse_socks", rendezvous_host="vps.example", rendezvous_port=443)
    sess = mint(p, ttl_minutes=30)
    original = render_plan(sess)
    # Simulate storage: snapshot the profile + ports, drop the secret entirely.
    params = profile_params(p)
    rebuilt = rebuild_session(params, socks_port=sess.socks_port, jump_port=sess.jump_port, ttl_minutes=30)
    assert rebuilt.rendezvous_password == ""  # never stored
    regenerated = render_plan(rebuilt)
    assert regenerated.anchor == original.anchor
    assert regenerated.traveler == original.traveler


# ── liveness predicate: ONE home ─────────────────────────────────────────────

def test_effective_status_killed_beats_everything():
    future = datetime(2999, 1, 1)
    assert effective_status(STATUS_KILLED, future, now=datetime(2000, 1, 1)) == STATUS_KILLED


def test_effective_status_expired_when_past_expiry():
    past = datetime(2000, 1, 1)
    assert effective_status(STATUS_ACTIVE, past, now=datetime(2024, 1, 1)) == STATUS_EXPIRED


def test_effective_status_active_before_expiry():
    now = datetime(2024, 1, 1)
    assert effective_status(STATUS_ACTIVE, now + timedelta(minutes=5), now=now) == STATUS_ACTIVE
    assert is_active(STATUS_ACTIVE, now + timedelta(minutes=5), now=now)


def test_is_listable_keeps_recently_died_then_ages_off():
    now = datetime(2024, 1, 1, 12, 0, 0)
    died_recently = now - timedelta(minutes=1)
    assert is_listable(died_recently, now=now)  # within grace -> still visible
    long_dead = now - LISTABLE_GRACE - timedelta(minutes=1)
    assert not is_listable(long_dead, now=now)  # aged off
