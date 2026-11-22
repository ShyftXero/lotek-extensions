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
    CREDENTIAL_EPHEMERAL,
    CREDENTIAL_KEY,
    CREDENTIAL_PASSWORD,
    LISTABLE_GRACE,
    MIN_PASSWORD_LENGTH,
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_KILLED,
    Profile,
    _operator_cmd_c,
    deviation_risks,
    effective_status,
    encode_command,
    gen_password,
    has_deviation_risk,
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

    assert "posix" in plan.anchor and "posix" in plan.operator
    anchor, operator = plan.anchor["posix"], plan.operator["posix"]
    assert f"-R 127.0.0.1:{sess.socks_port}" in anchor
    assert "-N" in anchor
    assert "ExitOnForwardFailure=yes" in anchor
    assert "UserKnownHostsFile=/dev/null" in anchor
    # the GENERATED per-pivot identity is what both legs dial (no shared literal user any more)
    assert f"{sess.ssh_user}@vps.example" in anchor
    assert f"-L {sess.socks_port}:127.0.0.1:{sess.socks_port}" in operator
    assert sess.ssh_password not in anchor
    assert sess.ssh_password not in operator
    assert any("Remote DNS" in n for n in plan.notes)


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
    operator = plan.operator["posix"]
    assert f"-D {sess.socks_port}" in operator
    assert f"{sess.ssh_user}@home.tail.net" in operator
    assert any("not remote DNS" in n for n in plan.notes)
    assert "NOT remote DNS" in operator


# ── topology C: relay + sshuttle, full-subnet L3 ─────────────────────────────

def test_topology_c_sshuttle_uses_proxyjump():
    p = _profile(
        "relay_sshuttle", rendezvous_host="vps.example", rendezvous_port=443,
        anchor_host="home-pi.local", anchor_user="pi", cidr="192.168.1.0/24",
    )
    sess = mint(p)
    plan = render_plan(sess)
    assert f"-R 127.0.0.1:{sess.jump_port}:127.0.0.1:22" in plan.anchor["posix"]
    travel = plan.operator["sshuttle"]
    assert "sshuttle --dns" in travel
    assert f"-e 'ssh -J {sess.ssh_user}@vps.example:443'" in travel
    assert f"-r pi@127.0.0.1:{sess.jump_port}" in travel
    assert "192.168.1.0/24" in travel
    assert any("sudo" in n for n in plan.notes)


def test_topology_c_sshuttle_auto_detects_when_no_cidr():
    p = _profile("relay_sshuttle", rendezvous_host="vps.example", anchor_host="home-pi.local", cidr="")
    plan = render_plan(mint(p))
    assert " -N" in plan.operator["sshuttle"]


# ── validation ──────────────────────────────────────────────────────────────

def test_relay_topology_without_rendezvous_rejected():
    # No rendezvous default ships, so a blank one must SAY what to do — not just name a field.
    with pytest.raises(ValueError, match="Rendezvous host is required"):
        render_plan(mint(_profile("relay_reverse_socks")))


def test_direct_topology_without_anchor_rejected():
    with pytest.raises(ValueError, match="[Aa]nchor host"):
        render_plan(mint(_profile("direct_socks")))


def test_topology_c_operator_rejects_non_sshuttle_renderer():
    sess = mint(_profile("relay_sshuttle", rendezvous_host="v", anchor_host="a"))
    with pytest.raises(ValueError, match="sshuttle-only"):
        _operator_cmd_c(sess, "posix")


# ── residue + port model ────────────────────────────────────────────────────

def test_random_port_in_ephemeral_range():
    for _ in range(1000):
        assert 1024 <= random_port() <= 65535


def test_password_never_on_any_command_line():
    sess = mint(_profile("relay_reverse_socks", rendezvous_host="vps.example"))
    plan = render_plan(sess)
    for cmd in [*plan.anchor.values(), *plan.operator.values()]:
        assert sess.ssh_password not in cmd


def test_password_is_at_least_18_visible_characters():
    """The credential model is ONE ephemeral random password shown once, so its length is the whole
    security budget. Pinned here: every draw is >= 18 visible (printable, ASCII, no whitespace)
    characters. There is deliberately no key generation — `ssh -i` needs a key file on the borrowed
    box, which is the residue a throwaway pivot must not leave."""
    assert MIN_PASSWORD_LENGTH >= 18
    for _ in range(200):
        pw = gen_password()
        assert len(pw) >= MIN_PASSWORD_LENGTH, pw
        assert pw.isprintable() and pw.isascii(), pw
        assert " " not in pw and "\n" not in pw, pw


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


# ── re-fetch: rebuild a stored session from its snapshot + stored identity ───

def test_rebuild_session_reproduces_commands_without_the_password():
    p = _profile("relay_reverse_socks", rendezvous_host="vps.example", rendezvous_port=443)
    sess = mint(p, ttl_minutes=30)
    original = render_plan(sess)
    # Simulate storage: the profile snapshot + the ports + the identity, and NO password (the command
    # set must not depend on it — it is pasted at the prompt, so it is never on argv).
    params = profile_params(p)
    rebuilt = rebuild_session(
        params, socks_port=sess.socks_port, jump_port=sess.jump_port, ttl_minutes=30,
        ssh_user=sess.ssh_user, ssh_password="",
    )
    assert rebuilt.ssh_password == ""
    regenerated = render_plan(rebuilt)
    assert regenerated.anchor == original.anchor
    assert regenerated.operator == original.operator
    # …and WITH the stored password the commands are still byte-identical (it never reaches argv).
    with_secret = render_plan(rebuild_session(
        params, socks_port=sess.socks_port, jump_port=sess.jump_port, ttl_minutes=30,
        ssh_user=sess.ssh_user, ssh_password=sess.ssh_password,
    ))
    assert with_secret.anchor == original.anchor and with_secret.operator == original.operator


# ── deviation-risk predicate: the ONE home behind the banner on both pages ──

# The secure default: a random high port on the operator's own box + a one-use ephemeral password.
_SECURE = dict(name="t", topology="relay_reverse_socks", rendezvous_host="vps.example",
               rendezvous_port=random_port())


def test_risk_banner_is_silent_on_the_secure_default():
    assert deviation_risks(Profile(**_SECURE)) == ()
    assert has_deviation_risk(Profile(**_SECURE)) is False


def test_risk_banner_is_silent_until_a_rendezvous_is_named():
    # What the mint page renders before the operator types a host: nothing is pinned anywhere -> silent.
    assert deviation_risks(Profile(name="", topology="relay_reverse_socks", rendezvous_host=None)) == ()


@pytest.mark.parametrize("port", [22, 443, 80, 2222])
def test_risk_banner_fires_on_a_pinned_well_known_port(port):
    risks = deviation_risks(Profile(**{**_SECURE, "rendezvous_port": port}))
    assert len(risks) == 1, risks
    assert str(port) in risks[0] and "vps.example" in risks[0]


def test_risk_banner_fires_on_a_non_random_privileged_port():
    # Anything < 1024 cannot have come from sshim's random 1024..65535 model.
    assert deviation_risks(Profile(**{**_SECURE, "rendezvous_port": 22}))
    assert deviation_risks(Profile(**{**_SECURE, "rendezvous_port": 1023}))


def test_risk_banner_fires_on_a_static_credential():
    # Phase 1 only ever mints the ephemeral secret; the static arms exist so the predicate is total and
    # a later slice introducing a reusable password / saved key MUST come through here.
    for kind, needle in ((CREDENTIAL_PASSWORD, "reusable password"), (CREDENTIAL_KEY, "non-ephemeral key")):
        risks = deviation_risks(Profile(**_SECURE), credential=kind)
        assert len(risks) == 1, risks
        assert needle in risks[0]
        assert has_deviation_risk(Profile(**_SECURE), credential=kind) is True
    # …and the one-use ephemeral password stays quiet, which is the shipped model.
    assert deviation_risks(Profile(**_SECURE), credential=CREDENTIAL_EPHEMERAL) == ()


def test_risk_banner_reports_the_port_and_the_credential_together():
    risks = deviation_risks(Profile(**{**_SECURE, "rendezvous_port": 443}), credential=CREDENTIAL_PASSWORD)
    assert len(risks) == 2


def test_risk_banner_judges_the_anchor_port_in_direct_mode():
    # Topology B has no rendezvous: the inbound surface IS the anchor's sshd.
    direct = dict(name="t", topology="direct_socks", anchor_host="home.tail.net", anchor_port=22)
    assert any("home.tail.net" in r for r in deviation_risks(Profile(**direct)))
    assert deviation_risks(Profile(**{**direct, "anchor_port": random_port()})) == ()


def test_an_unknown_credential_kind_raises_instead_of_passing_silently():
    with pytest.raises(ValueError, match="credential must be one of"):
        deviation_risks(Profile(**_SECURE), credential="kerberos-magic")


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


def test_the_ssh_port_is_an_option_before_the_host_everywhere():
    """Regression: `-p <port>` must be an ssh OPTION, before `--` and the host.

    It used to be emitted as `... -- user@host -p PORT`, where `--` ends option parsing:
    ssh then ignored the port entirely and dialled 22, so a session minted for a
    non-standard rendezvous port silently connected to the wrong service (or nowhere).
    Caught by the hhr2 e2e (curl never came back through the pivot).
    """
    import re

    cases = [
        (
            Profile(name="a", topology="relay_reverse_socks",
                    rendezvous_host="rdv.example", rendezvous_port=22022),
            22022,
        ),
        (Profile(name="b", topology="direct_socks",
                 anchor_host="anchor.example", anchor_port=2222), 2222),
        (
            Profile(name="c", topology="relay_sshuttle", rendezvous_host="rdv.example",
                    rendezvous_port=22022, anchor_host="a.example"),
            22022,
        ),
    ]
    for prof, port in cases:
        pl = render_plan(mint(prof))
        for leg_name in ("anchor", "operator"):
            for renderer, cmd in getattr(pl, leg_name).items():
                if renderer == "sshuttle":
                    continue  # sshuttle wraps ssh via -J host:port, not a bare -p
                if renderer == "powershell_encoded":
                    import base64
                    blob = cmd.split("EncodedCommand ", 1)[1].split("  #", 1)[0].strip()
                    cmd = base64.b64decode(blob).decode("utf-16-le")
                head, sep, tail = cmd.partition(" -- ")
                assert sep, f"{leg_name}/{renderer}: no `--` separator: {cmd}"
                assert re.search(rf"-p {port}\b", head), (
                    f"{leg_name}/{renderer}: `-p {port}` must sit BEFORE `--`: {cmd}"
                )
                assert f"-p {port}" not in tail.replace("  #", "\n#"), (
                    f"{leg_name}/{renderer}: `-p {port}` must not land in the host/command tail: {cmd}"
                )
