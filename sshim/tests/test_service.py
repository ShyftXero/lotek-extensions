"""Pin the service layer: the ONE tenancy rule, sealed-secret regeneration, the liveness window, TTL clamp.

A stub DB proves this LOGIC; the MOUNT (PAT identity -> g.principal) is pinned in lotek's suite.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from sshim import plan, secretbox, service
from sshim.service import (
    Denied,
    Invalid,
    kill_session,
    load_visible,
    mint_session,
    now_utc,
    plan_for,
    profile_from_fields,
    visible_sessions,
)


def _prof(**kw):
    base = dict(name="t", topology="relay_reverse_socks", rendezvous_host="vps.example", rendezvous_port=443)
    base.update(kw)
    return profile_from_fields(**base)


def _mint(db, owner, key_path, **kw):
    prof = kw.pop("prof", None) or _prof()
    return mint_session(
        db, prof, owner_id=owner, standalone=False,
        ttl_minutes=kw.pop("ttl_minutes", 30), max_ttl=kw.pop("max_ttl", 240),
        secret_key_path=key_path, **kw,
    )


# ── tenancy: owner-only, admin sees all, no existence oracle ──────────────────

def test_owner_sees_own_session_others_do_not(db, owner, other, key_path):
    row, _secret = _mint(db, owner, key_path)
    assert load_visible(db, row.id, owner_id=owner, is_admin=False) is not None
    # a non-owner gets None (rendered 404, never 403) — INV-TENANCY-01
    assert load_visible(db, row.id, owner_id=other, is_admin=False) is None
    # …but an admin sees it
    assert load_visible(db, row.id, owner_id=other, is_admin=True) is not None


def test_visible_sessions_scoped_to_owner(db, owner, other, key_path):
    _mint(db, owner, key_path)
    assert len(visible_sessions(db, owner_id=owner, is_admin=False)) == 1
    assert visible_sessions(db, owner_id=other, is_admin=False) == []
    assert len(visible_sessions(db, owner_id=other, is_admin=True)) == 1


def test_anonymous_owner_matches_nothing(db, owner, key_path):
    _mint(db, owner, key_path)
    assert visible_sessions(db, owner_id=None, is_admin=False) == []


# ── mint requires a resolvable identity when mounted ──────────────────────────

def test_mint_without_identity_when_mounted_is_denied(db, key_path):
    with pytest.raises(Denied):
        mint_session(db, _prof(), owner_id=None, standalone=False, ttl_minutes=30, max_ttl=240,
                     secret_key_path=key_path)


def test_mint_clamps_ttl_to_max(db, owner, key_path):
    row, _ = _mint(db, owner, key_path, ttl_minutes=9999, max_ttl=240)
    assert row.ttl_minutes == 240
    # expiry reflects the clamped ttl, not the requested one
    assert row.expires_at <= now_utc() + timedelta(minutes=240, seconds=5)


# ── the row holds CIPHERTEXT; the owner re-fetches it back ───────────────────

def test_plan_regenerates_the_stored_password_for_the_owner(db, owner, key_path):
    row, secret = _mint(db, owner, key_path)
    assert secret and len(secret) >= plan.MIN_PASSWORD_LENGTH
    # the row stores CIPHERTEXT: neither the profile snapshot nor the column carries the cleartext
    assert secret not in str(row.params)
    assert row.ssh_password and secret not in row.ssh_password
    assert service.reveal_password(row, secret_key_path=key_path) == secret
    regenerated = plan_for(row, secret_key_path=key_path)
    assert regenerated.session.ssh_password == secret
    # …and the password still never reaches a command
    for cmd in [*regenerated.anchor.values(), *regenerated.operator.values()]:
        assert secret not in cmd
    assert any("vps.example" in c for c in regenerated.anchor.values())


def test_an_unreadable_key_fails_closed_instead_of_showing_garbage(db, owner, key_path, tmp_path):
    """No key / wrong key => `None` (the surface offers a re-mint). It must never print a wrong secret."""
    row, secret = _mint(db, owner, key_path)
    foreign = secretbox.key_path(tmp_path / "someone-elses-instance")
    assert service.reveal_password(row, secret_key_path=foreign) is None
    assert service.reveal_password(row, secret_key_path=tmp_path / "missing-dir" / "nope.key") is None
    payload = service.plan_to_dict(plan_for(row, secret_key_path=foreign))
    assert payload["secret_available"] is False
    assert payload["ssh_password"] is None
    assert secret not in str(payload)
    assert "re-mint" in payload["secret_note"].lower()


def test_plan_to_dict_carries_the_secret_when_it_can_be_opened(db, owner, key_path):
    row, secret = _mint(db, owner, key_path)
    payload = service.plan_to_dict(plan_for(row, secret_key_path=key_path))
    assert payload["secret_available"] is True
    assert payload["ssh_password"] == secret
    assert payload["ssh_user"] == row.ssh_user


def test_the_list_serialisation_never_carries_the_password(db, owner, key_path):
    row, secret = _mint(db, owner, key_path)
    payload = service.session_to_dict(row)
    assert "ssh_password" not in payload
    assert secret not in str(payload)
    assert payload["ssh_user"] == row.ssh_user and payload["kill_at"] is None


# ── liveness window: expired stays listable briefly, then ages off ────────────

def test_expired_session_stays_listable_within_grace_then_drops(db, owner, key_path):
    row, _ = _mint(db, owner, key_path)
    now = now_utc()
    # just-expired (within grace) -> still listed, status expired
    row.expires_at = now - timedelta(minutes=1)
    db.commit()
    listed = visible_sessions(db, owner_id=owner, is_admin=False, now=now)
    assert len(listed) == 1
    assert plan.effective_status(listed[0].status, listed[0].expires_at, now=now) == plan.STATUS_EXPIRED
    # long-dead (past grace) -> aged off
    row.expires_at = now - plan.LISTABLE_GRACE - timedelta(minutes=5)
    db.commit()
    assert visible_sessions(db, owner_id=owner, is_admin=False, now=now) == []


# ── kill records the explicit action; status derives killed ───────────────────

def test_kill_sets_status_and_is_reflected_in_effective_status(db, owner, key_path):
    row, _ = _mint(db, owner, key_path)
    kill_session(db, row, owner_id=owner, is_admin=False)
    reloaded = load_visible(db, row.id, owner_id=owner, is_admin=False)
    assert reloaded is not None
    assert reloaded.status == plan.STATUS_KILLED
    assert plan.effective_status(reloaded.status, reloaded.expires_at, now=now_utc()) == plan.STATUS_KILLED


def test_kill_others_session_denied(db, owner, other, key_path):
    row, _ = _mint(db, owner, key_path)
    with pytest.raises(Denied):
        kill_session(db, row, owner_id=other, is_admin=False)


# ── validation routes through plan, surfaced as Invalid ───────────────────────

def test_bad_topology_is_invalid():
    with pytest.raises(Invalid):
        profile_from_fields(name="t", topology="nope", rendezvous_host="x")


def test_relay_without_rendezvous_is_invalid():
    # No rendezvous default ships, so the message must tell the operator what to DO.
    with pytest.raises(Invalid, match="Rendezvous host is required"):
        profile_from_fields(name="t", topology="relay_reverse_socks")


# ── shell-injection guard: host/user/cidr reach a command the operator pastes ──

@pytest.mark.parametrize("evil", ["evil.com; rm -rf /", "a$(id)", "a b", "x|nc", "`whoami`", "a&b"])
def test_malicious_rendezvous_host_rejected(evil):
    with pytest.raises(Invalid):
        profile_from_fields(name="t", topology="relay_reverse_socks", rendezvous_host=evil)


def test_malicious_user_and_cidr_rejected():
    with pytest.raises(Invalid):
        profile_from_fields(name="t", topology="relay_reverse_socks",
                            rendezvous_host="vps.example", rendezvous_user="root;id")
    with pytest.raises(Invalid):
        profile_from_fields(name="t", topology="relay_sshuttle", rendezvous_host="v",
                            anchor_host="a", cidr="10.0.0.0/8;reboot")


def test_legitimate_host_user_cidr_accepted():
    p = profile_from_fields(name="t", topology="relay_sshuttle", rendezvous_host="vps.example-1.net",
                            rendezvous_user="sshim_bot", anchor_host="10.0.0.5", cidr="192.168.1.0/24")
    assert p.rendezvous_host == "vps.example-1.net" and p.cidr == "192.168.1.0/24"
