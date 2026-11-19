"""The container seam: a mint rides a core-hosted single-use relay when the host offers one.

WHY THIS FILE EXISTS. sshim generates a random ssh_user/ssh_password per pivot. On an external
rendezvous that account exists nowhere — the extension cannot mint accounts on somebody else's sshd.
The whole point of the Phase-2 relay container (the `lotek-pivot-sshd` image takes USER_NAME /
USER_PASSWORD) is that it PROVISIONS the generated identity, so the random credential becomes real and
single-use. Measured before this file: `grep exclusive_container_acquire sshim/` returned only
comments, so the primary profile dialled the dashboard host's own sshd on :22 and the generated
username named nothing.

The tests split the two modes deliberately:
  * hook present  -> the plan dials the CONTAINER, and the acquire call carries this session's identity
  * hook absent   -> byte-identical Phase-1 behaviour (the operator's own host)
and they pin that teardown gives the container back without ever blocking the kill.
"""
from __future__ import annotations

import pytest

from sshim import deps as DEP
from sshim import plan, service


def _prof(**kw) -> plan.Profile:
    base = {
        "name": "t",
        "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example",
        "rendezvous_port": 22,
        "rendezvous_user": "sshim",
    }
    base.update(kw)
    return plan.Profile(**base)


def _mint(db, tmp_path, prof=None, acquire=None):
    return service.mint_session(
        db, prof or _prof(),
        owner_id=None, standalone=True,
        ttl_minutes=5, max_ttl=60,
        secret_key_path=tmp_path / "seal.key",
        acquire=acquire,
    )


# ── the acquire path ────────────────────────────────────────────────────────


def test_acquire_is_called_and_the_plan_dials_the_container(session_factory, tmp_path):
    seen = []

    def acquire(sess):
        seen.append(sess)
        return {"host": "relay.internal", "port": 22022, "handle_id": "h-1"}

    with session_factory() as db:
        row, _secret = _mint(db, tmp_path, acquire=acquire)
        assert len(seen) == 1, "acquire must be called exactly once per mint"
        assert service.relay_mode(row.params) == service.RELAY_CONTAINER
        assert row.rendezvous_host == "relay.internal"
        assert (row.params or {}).get('rendezvous_port') == 22022

        rendered = service.plan_for(row, secret_key_path=tmp_path / "seal.key")
        anchor = rendered.anchor["posix"]
        assert "relay.internal" in anchor and "-p 22022" in anchor
        assert "vps.example" not in anchor, "the operator's typed host must not survive the override"


def test_acquire_receives_this_sessions_generated_identity(session_factory, tmp_path):
    seen = {}

    def acquire(sess):
        seen["user"] = sess.ssh_user
        seen["password"] = sess.ssh_password
        return {"host": "relay.internal", "port": 22022, "handle_id": "h-1"}

    with session_factory() as db:
        row, secret = _mint(db, tmp_path, acquire=acquire)
        # the container is told WHICH account to provision, and it is this session's own
        assert seen["user"] == row.ssh_user
        assert seen["user"] and not seen["user"][0].isdigit()
        assert len(seen["password"]) >= 18
        assert seen["password"] == secret


def test_a_useless_acquire_answer_falls_back_to_the_typed_host(session_factory, tmp_path):
    """Never mint a pivot pointed at nothing: a malformed acquire result means Phase 1."""
    with session_factory() as db:
        row, _ = _mint(db, tmp_path, acquire=lambda sess: {"nonsense": True})
        assert service.relay_mode(row.params) == service.RELAY_EXTERNAL
        assert row.rendezvous_host == "vps.example"
        assert service.relay_handle(row.params) is None


def test_an_acquire_that_raises_does_not_abort_the_mint(session_factory, tmp_path):
    def boom(sess):
        raise RuntimeError("host exploded")

    with session_factory() as db:
        row, _ = _mint(db, tmp_path, acquire=boom)
        assert service.relay_mode(row.params) == service.RELAY_EXTERNAL
        assert row.rendezvous_host == "vps.example"


def test_without_the_hook_the_plan_is_phase_one(session_factory, tmp_path):
    """Regression: no hook => the operator's own rendezvous, exactly as before."""
    with session_factory() as db:
        row, _ = _mint(db, tmp_path, acquire=None)
        assert service.relay_mode(row.params) == service.RELAY_EXTERNAL
        rendered = service.plan_for(row, secret_key_path=tmp_path / "seal.key")
        assert "vps.example" in rendered.anchor["posix"]


def test_direct_topology_never_acquires(session_factory, tmp_path):
    """Direct talks to the anchor itself — there is nothing for a relay container to host."""
    calls = []
    with session_factory() as db:
        _mint(db, tmp_path,
              prof=_prof(topology="direct_socks", rendezvous_host=None, anchor_host="a.example"),
              acquire=lambda s: calls.append(s))
        assert calls == []


# ── teardown ────────────────────────────────────────────────────────────────


@pytest.fixture
def client_with_hooks(tmp_path):
    """A mounted-enough app whose host injects the two container hooks."""
    from flask import Flask
    from sqlalchemy import create_engine

    import sshim

    app = Flask(__name__)
    app.secret_key = "test"  # noqa: S105 — throwaway test app
    app.config["PROPAGATE_EXCEPTIONS"] = True
    engine = create_engine("sqlite:///:memory:")
    sshim.register(app, engine, instance_path=str(tmp_path / "inst"))

    released: list[str] = []
    acquired: list[dict] = []

    def acquire(sess=None, **kw):
        # mint_session calls the hook with the session positionally
        acquired.append({"user": getattr(sess, "ssh_user", None), **kw})
        return {"host": "relay.internal", "port": 22022, "handle_id": "h-kill"}

    def release(handle):
        released.append(handle)

    cfg = app.extensions["sshim"]
    cfg.extras = {**(cfg.extras or {}),
                  **{DEP.PHASE2_EXCLUSIVE_ACQUIRE: acquire, DEP.PHASE2_EXCLUSIVE_RELEASE: release}}
    with app.test_client() as c:
        yield c, acquired, released


def test_kill_releases_the_recorded_handle(client_with_hooks):
    client, acquired, released = client_with_hooks
    resp = client.post("/sshim/mint", data={
        "profile_id": "", "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": "22",
        "rendezvous_user": "sshim", "ttl_minutes": "5",
    })
    assert resp.status_code in (200, 201)
    assert acquired, "the mint should have acquired a container"
    import re
    sid = re.search(r"/sshim/sessions/([0-9a-f-]{36})", resp.get_data(as_text=True)).group(1)
    assert client.post(f"/sshim/sessions/{sid}/kill", data={"confirm": "1"}).status_code in (302, 303)
    assert released == ["h-kill"], "the container must be given back exactly once"


def test_kill_succeeds_even_when_release_raises(client_with_hooks):
    client, _acquired, _released = client_with_hooks
    resp = client.post("/sshim/mint", data={
        "profile_id": "", "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": "22",
        "rendezvous_user": "sshim", "ttl_minutes": "5",
    })
    import re
    sid = re.search(r"/sshim/sessions/([0-9a-f-]{36})", resp.get_data(as_text=True)).group(1)

    def boom(handle):
        raise RuntimeError("host already reaped it")

    client.application.extensions["sshim"].extras[DEP.PHASE2_EXCLUSIVE_RELEASE] = boom
    # a teardown the host already performed must NOT block the operator's kill
    assert client.post(f"/sshim/sessions/{sid}/kill", data={"confirm": "1"}).status_code in (302, 303)


def test_the_detail_page_names_the_relay_mode(client_with_hooks):
    client, _acquired, _released = client_with_hooks
    html = client.post("/sshim/mint", data={
        "profile_id": "", "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": "22",
        "rendezvous_user": "sshim", "ttl_minutes": "5",
    }).get_data(as_text=True)
    assert "core-hosted single-use container" in html
    assert "must already exist there" not in html
