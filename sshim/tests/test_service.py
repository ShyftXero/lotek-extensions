"""Pin the service layer: the ONE tenancy rule, secret-free regeneration, the liveness window, TTL clamp.

A stub DB proves this LOGIC; the MOUNT (PAT identity -> g.principal) is pinned in lotek's suite.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from sshim import plan, service
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


def _mint(db, owner, **kw):
    prof = kw.pop("prof", None) or _prof()
    return mint_session(
        db, prof, owner_id=owner, standalone=False,
        ttl_minutes=kw.pop("ttl_minutes", 30), max_ttl=kw.pop("max_ttl", 240), **kw,
    )


# ── tenancy: owner-only, admin sees all, no existence oracle ──────────────────

def test_owner_sees_own_session_others_do_not(db, owner, other):
    row, _secret = _mint(db, owner)
    assert load_visible(db, row.id, owner_id=owner, is_admin=False) is not None
    # a non-owner gets None (rendered 404, never 403) — INV-TENANCY-01
    assert load_visible(db, row.id, owner_id=other, is_admin=False) is None
    # …but an admin sees it
    assert load_visible(db, row.id, owner_id=other, is_admin=True) is not None


def test_visible_sessions_scoped_to_owner(db, owner, other):
    _mint(db, owner)
    assert len(visible_sessions(db, owner_id=owner, is_admin=False)) == 1
    assert visible_sessions(db, owner_id=other, is_admin=False) == []
    assert len(visible_sessions(db, owner_id=other, is_admin=True)) == 1


def test_anonymous_owner_matches_nothing(db, owner):
    _mint(db, owner)
    assert visible_sessions(db, owner_id=None, is_admin=False) == []


# ── mint requires a resolvable identity when mounted ──────────────────────────

def test_mint_without_identity_when_mounted_is_denied(db):
    with pytest.raises(Denied):
        mint_session(db, _prof(), owner_id=None, standalone=False, ttl_minutes=30, max_ttl=240)


def test_mint_clamps_ttl_to_max(db, owner):
    row, _ = _mint(db, owner, ttl_minutes=9999, max_ttl=240)
    assert row.ttl_minutes == 240
    # expiry reflects the clamped ttl, not the requested one
    assert row.expires_at <= now_utc() + timedelta(minutes=240, seconds=5)


# ── secret-free re-fetch: the password is never stored, commands regenerate ────

def test_plan_regenerates_without_the_secret(db, owner):
    row, secret = _mint(db, owner)
    assert secret and len(secret) > 10
    # nothing on the row carries the cleartext secret
    assert secret not in str(row.params)
    regenerated = plan_for(row)
    for cmd in [*regenerated.anchor.values(), *regenerated.operator.values()]:
        assert secret not in cmd
    # and the regenerated commands point at the stored rendezvous
    assert any("vps.example" in c for c in regenerated.anchor.values())


def test_plan_to_dict_declares_secret_unavailable(db, owner):
    row, _ = _mint(db, owner)
    payload = service.plan_to_dict(plan_for(row))
    assert payload["secret_available"] is False
    assert "re-mint" in payload["secret_note"].lower()


# ── liveness window: expired stays listable briefly, then ages off ────────────

def test_expired_session_stays_listable_within_grace_then_drops(db, owner):
    row, _ = _mint(db, owner)
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

def test_kill_sets_status_and_is_reflected_in_effective_status(db, owner):
    row, _ = _mint(db, owner)
    kill_session(db, row, owner_id=owner, is_admin=False)
    reloaded = load_visible(db, row.id, owner_id=owner, is_admin=False)
    assert reloaded is not None
    assert reloaded.status == plan.STATUS_KILLED
    assert plan.effective_status(reloaded.status, reloaded.expires_at, now=now_utc()) == plan.STATUS_KILLED


def test_kill_others_session_denied(db, owner, other):
    row, _ = _mint(db, owner)
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
