"""Pin the per-pivot identity, the sealed-at-rest password, the opt-in kill date, and the reap.

These are the behaviours that CHANGED in this slice, so they are pinned end to end rather than one
predicate at a time:

* every session gets its OWN generated ``ssh_user``/``ssh_password`` (no shared literal user), threaded
  into BOTH legs, with an explicit profile pin winning;
* the password is stored SEALED (ciphertext in the row, key in the instance dir) and the owner can
  re-fetch it — while the LIST surfaces never carry it at all;
* ``kill_at`` is an extra, opt-in bound on top of the TTL: past it, the status is terminal ``killed``
  and the plan is refused;
* the throttled sweep reaps kill dates and is idempotent.

The Flask bits use a stub app with the host's PAT hooks injected in ``extras`` — the MOUNT itself
(PAT identity -> g.principal) is pinned in lotek's suite.
"""
from __future__ import annotations

import base64
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import create_engine

import sshim
from sshim import plan, secretbox, service
from sshim.plan import (
    MAX_GENERATED_USER_LENGTH,
    MIN_PASSWORD_LENGTH,
    STATUS_ACTIVE,
    STATUS_KILLED,
    Profile,
    anchor_identity,
    effective_status,
    gen_user,
    mint,
    render_plan,
    rendezvous_identity,
)
from sshim.service import (
    SessionDead,
    mint_session,
    now_utc,
    parse_kill_at,
    plan_for,
    profile_from_fields,
    reveal_password,
    sweep_expired,
)

_OWNER = uuid.uuid7()


def _profile(topology="relay_reverse_socks", **kw) -> Profile:
    base = dict(name="t", topology=topology, rendezvous_host="vps.example", rendezvous_port=443)
    base.update(kw)
    return Profile(**base)


def _plain(cmd: str) -> str:
    """The command a renderer actually runs — decoded, so a substring pin works for every renderer.

    ``powershell_encoded`` is base64(UTF-16LE), so the username is IN the string but not readable in it;
    asserting on the encoded blob would prove nothing about which identity the leg dials."""
    if not cmd.startswith("powershell -NoProfile -EncodedCommand "):
        return cmd
    blob = cmd.split("EncodedCommand ", 1)[1].split("  #", 1)[0].strip()
    return base64.b64decode(blob).decode("utf-16-le")


def _mint(db, key_path, **kw):
    prof = kw.pop("prof", None) or profile_from_fields(
        name="t", topology="relay_reverse_socks", rendezvous_host="vps.example", rendezvous_port=443
    )
    return mint_session(
        db, prof, owner_id=_OWNER, standalone=False,
        ttl_minutes=kw.pop("ttl_minutes", 30), max_ttl=240, secret_key_path=key_path, **kw,
    )


# ── (a) two mints are two identities ─────────────────────────────────────────

def test_two_mints_get_different_users_and_passwords():
    p = _profile()
    first, second = mint(p), mint(p)
    assert first.ssh_user != second.ssh_user
    assert first.ssh_password != second.ssh_password


# ── (b) the generated user is a legal username on Linux AND Windows OpenSSH ──

def test_the_generated_user_is_a_legal_username():
    for _ in range(200):
        user = gen_user()
        assert user[0].isalpha(), f"a username cannot start with a digit: {user}"
        assert re.fullmatch(r"[a-z][a-z0-9]*", user), user
        assert len(user) <= MAX_GENERATED_USER_LENGTH == 16, user
        # a Linux useradd name and a Windows OpenSSH account agree on this shape
        assert not user.startswith("-") and "_" not in user and user.lower() == user


# ── (c) the password keeps the existing minimum length ───────────────────────

def test_the_generated_password_is_at_least_18_visible_chars():
    assert MIN_PASSWORD_LENGTH >= 18
    for _ in range(200):
        pw = mint(_profile()).ssh_password
        assert len(pw) >= MIN_PASSWORD_LENGTH, pw
        assert pw.isprintable() and pw.isascii() and " " not in pw, pw


# ── (d) ONE identity, BOTH legs ─────────────────────────────────────────────

@pytest.mark.parametrize("topology", ["relay_reverse_socks", "relay_sshuttle"])
def test_the_generated_identity_appears_in_both_legs(topology):
    kw = dict(rendezvous_host="vps.example")
    if topology == "relay_sshuttle":
        kw["anchor_host"] = "home-pi.local"
    sess = mint(_profile(topology=topology, **kw))
    rendered = render_plan(sess)
    assert rendezvous_identity(sess.profile, sess) == sess.ssh_user
    anchor_cmds = [c for c in rendered.anchor.values()]
    operator_cmds = [c for c in rendered.operator.values()]
    assert anchor_cmds and operator_cmds
    for cmd in anchor_cmds + operator_cmds:
        assert f"{sess.ssh_user}@vps.example" in _plain(cmd)
    # the generated password never rides along on either leg
    for cmd in anchor_cmds + operator_cmds:
        assert sess.ssh_password not in _plain(cmd)


# ── (e) an explicit profile user wins over the generated one ────────────────

def test_an_explicit_profile_user_overrides_the_generated_one():
    pinned = _profile(rendezvous_user="deploy")
    sess = mint(pinned)
    assert rendezvous_identity(pinned, sess) == "deploy"
    rendered = render_plan(sess)
    for cmd in [*rendered.anchor.values(), *rendered.operator.values()]:
        assert "deploy@vps.example" in _plain(cmd)
        assert sess.ssh_user not in _plain(cmd)
    # the anchor pin is its own field: topology B dials the ANCHOR identity
    direct = mint(_profile("direct_socks", anchor_host="box.tail.net", anchor_user="pi"))
    assert anchor_identity(direct.profile, direct) == "pi"
    assert "pi@box.tail.net" in render_plan(direct).operator["posix"]


# ── (f) the owner re-fetches the stored password ────────────────────────────

def test_the_owner_recovers_the_password_on_refetch(db, key_path):
    row, secret = _mint(db, key_path)
    # at rest it is CIPHERTEXT, not the cleartext secret
    assert row.ssh_password and row.ssh_password != secret and secret not in row.ssh_password
    # …and the owner gets it back, through the ONE reveal path the detail surfaces use
    assert reveal_password(row, secret_key_path=key_path) == secret
    regenerated = plan_for(row, secret_key_path=key_path)
    assert regenerated.session.ssh_user == row.ssh_user
    assert regenerated.session.ssh_password == secret


def test_a_foreign_or_missing_key_fails_closed(tmp_path, key_path, db):
    row, secret = _mint(db, key_path)
    assert reveal_password(row, secret_key_path=tmp_path / "other" / "k.key") is None
    tampered = row.ssh_password[:-4] + "AAAA"
    row.ssh_password = tampered
    assert reveal_password(row, secret_key_path=key_path) is None
    assert secret not in str(service.plan_to_dict(plan_for(row, secret_key_path=key_path)))


# ── (g) kill_at in the past => terminal, plan refused ──────────────────────

def test_a_past_kill_at_yields_a_killed_session_whose_plan_is_refused(db, key_path):
    past = now_utc() - timedelta(minutes=1)
    row, _secret = _mint(db, key_path, kill_at=past)
    assert row.kill_at == past
    assert effective_status(row.status, row.expires_at, now=now_utc(), kill_at=row.kill_at) == STATUS_KILLED
    assert service.session_to_dict(row)["status"] == STATUS_KILLED
    with pytest.raises(SessionDead):
        plan_for(row, secret_key_path=key_path)


def test_kill_at_is_opt_in_and_never_replaces_the_ttl(db, key_path):
    row, _secret = _mint(db, key_path)
    assert row.kill_at is None  # blank form field => no kill date
    assert row.expires_at > now_utc()
    # a FUTURE kill date that lands before the TTL shortens the live window (both bounds still apply)
    soon = now_utc() + timedelta(minutes=5)
    row2, _ = _mint(db, key_path, kill_at=soon)
    assert plan.dies_at(row2.expires_at, kill_at=row2.kill_at) == soon
    assert effective_status(row2.status, row2.expires_at, now=soon, kill_at=row2.kill_at) == STATUS_KILLED
    # before it, the session is still live, TTL untouched
    quiet = now_utc()
    assert effective_status(row2.status, row2.expires_at, now=quiet, kill_at=row2.kill_at) == STATUS_ACTIVE
    assert row2.ttl_minutes == 30


def test_the_kill_at_field_parses_and_rejects_garbage():
    assert parse_kill_at("") is None and parse_kill_at(None) is None
    assert parse_kill_at("2031-01-02T03:04").isoformat() == "2031-01-02T03:04:00"
    assert parse_kill_at("2031-01-02T03:04:00+00:00").tzinfo is None  # normalised to naive UTC
    with pytest.raises(service.Invalid):
        parse_kill_at("next tuesday")


# ── (h) the sweep is idempotent ────────────────────────────────────────────

def test_the_sweep_is_idempotent(db, key_path):
    past = now_utc() - timedelta(minutes=1)
    row, _ = _mint(db, key_path, kill_at=past)
    first = sweep_expired(db)
    assert first["killed"] == 1
    reloaded = db.get(type(row), row.id)
    assert reloaded.status == STATUS_KILLED
    # a second sweep (a concurrent request, the cron job) has nothing left to transition
    second = sweep_expired(db)
    assert second == {"killed": 0, "expired": 0}
    assert db.get(type(row), row.id).status == STATUS_KILLED
    # and it never WRITES `expired` — that stays derived, which is why the sweep can be a no-op
    future = _mint(db, key_path)[0]
    future.expires_at = now_utc() - timedelta(seconds=1)
    db.commit()
    assert sweep_expired(db)["expired"] == 1
    assert db.get(type(future), future.id).status == STATUS_ACTIVE


def test_maybe_sweep_is_throttled_per_process(db, key_path):
    service._last_sweep_at = 0.0  # pretend a fresh process
    assert service.maybe_sweep(db) is not None  # the first page load sweeps
    assert service.maybe_sweep(db) is None      # …and the next one, inside the window, is skipped
    # an explicit sweep is always available to the operator/cron path
    assert sweep_expired(db) == {"killed": 0, "expired": 0}


# ── the Flask surfaces: the list hides the secret, the detail reveals it ────

@pytest.fixture
def client(tmp_path):
    """A stub mounted app with the host's PAT hooks injected in ``extras`` (the mount itself is lotek's)."""
    app = Flask(__name__)
    app.secret_key = "test"  # noqa: S105 - throwaway test app
    engine = create_engine("sqlite:///:memory:")
    cfg = sshim.register(app, engine, instance_path=str(tmp_path))
    cfg.extras.update({
        "pat_authenticate": lambda: None,
        "pat_actor": lambda: SimpleNamespace(id=_OWNER, username="op", role="operator"),
        "require_pat_scope": lambda scope: (lambda fn: fn),
        "audit": None,
    })
    with app.test_client() as c:
        yield c


def _mint_through_api(client):
    resp = client.post("/sshim/machine/sessions", json={
        "name": "t", "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": 443, "ttl_minutes": 30,
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)[:400]
    return resp.get_json()


# ── (i) the list endpoint never returns a password ──────────────────────────

def test_the_machine_list_never_returns_a_password(client):
    minted = _mint_through_api(client)
    secret = minted["ssh_password"]
    listing = client.get("/sshim/machine/sessions").get_json()
    assert len(listing["sessions"]) == 1
    body = str(listing)
    assert secret not in body
    assert "ssh_password" not in body
    # the username is not a secret: an operator needs it to pick the login, so it IS listed
    assert listing["sessions"][0]["ssh_user"] == minted["session"]["ssh_user"]


def test_the_single_session_detail_reveals_it_to_its_owner(client):
    minted = _mint_through_api(client)
    detail = client.get(f"/sshim/machine/sessions/{minted['session']['id']}").get_json()
    assert detail["plan"]["ssh_password"] == minted["ssh_password"]
    assert detail["plan"]["secret_available"] is True
    assert detail["plan"]["ssh_user"] == minted["ssh_user"]


def test_the_cli_forces_a_reap(tmp_path):
    app = Flask(__name__)
    engine = create_engine("sqlite:///:memory:")
    sshim.register(app, engine, instance_path=str(tmp_path))
    from sshim import models

    with app.app_context(), app.extensions["sshim"].session_factory() as db:
        row, _ = _mint(db, secretbox.key_path(tmp_path), kill_at=now_utc() - timedelta(minutes=1))
        row_id = row.id
    result = app.test_cli_runner().invoke(args=["sshim-reap"])
    assert result.exit_code == 0, result.output
    assert "killed=1" in result.output
    with app.app_context(), app.extensions["sshim"].session_factory() as db:
        assert db.get(models.Session, row_id).status == STATUS_KILLED


def test_the_detail_page_refuses_a_killed_session(client):
    """Page-level pin: a reachable kill date must not leave commands (or a credential) on the page."""
    resp = client.post("/sshim/mint", data={
        "name": "t", "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": "443",
        "ttl_minutes": "30", "kill_at": "2001-01-01T00:00",
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)[:400]
    html = resp.get_data(as_text=True)
    assert "no longer served" in html
    assert 'id="secret"' not in html, "a killed pivot must not display its credential"
    assert "ssh -N" not in html, "a killed pivot must not display live commands"


def test_the_mint_page_shows_the_generated_identity_and_stores_it_sealed(client):
    resp = client.post("/sshim/mint", data={
        "name": "t", "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": "443", "ttl_minutes": "30",
    })
    assert resp.status_code == 201
    html = resp.get_data(as_text=True)
    found = re.search(r'/sshim/sessions/([0-9a-f-]+)/kill', html)
    assert found, "the mint response must link its kill route"
    # the same identity is re-shown on the owner's re-fetch, and the user is in the commands
    detail = client.get(f"/sshim/sessions/{found.group(1)}").get_data(as_text=True)
    user = re.search(r'id="ssh-user">(p[0-9a-f]+)<', html)
    assert user, html[:600]
    assert f"{user.group(1)}@vps.example" in detail
    assert user.group(1) in detail.split('id="ssh-user">')[1][:64]


def test_only_the_identity_predicates_read_the_pinned_user():
    """AST drift guard: a command builder must NOT read `profile.rendezvous_user` / `.anchor_user`
    itself — that re-forks the 'explicit pin beats the generated identity' rule, and the two legs would
    silently drift apart. It was collapsed into `rendezvous_identity` / `anchor_identity`; keep it there.

    `profile_params` is allowed: it SNAPSHOTS the pin for storage (it must not resolve an identity), and
    a builder that read the pin itself would show up here."""
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / "sshim" / "plan.py").read_text()
    tree = ast.parse(source)
    readers = set()
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and node.attr in ("rendezvous_user", "anchor_user"):
                readers.add(fn.name)
    assert readers == {"rendezvous_identity", "anchor_identity", "profile_params"}, readers
