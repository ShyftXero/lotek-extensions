"""The container seam: a mint rides a core-hosted single-use relay when the host offers one.

WHY THIS FILE EXISTS, AND WHY IT IS WRITTEN THIS WAY. sshim generates a random ssh_user/ssh_password per
pivot. That identity only EXISTS if something provisions it, and an external sshd cannot — an operator's
box has whatever accounts it has. The relay container is what makes the random credential real
(lotek-pivot-sshd reads USER_NAME/USER_PASSWORD).

The first version of this wiring was WRONG IN A WAY STUB TESTS COULD NOT SEE, so the tests below are
deliberately shaped around those failures:

  * the call was POSITIONAL (``acquire(sess)``) against a keyword-only host hook, so core raised and the
    caller swallowed it into a silent Phase-1 fallback — the container never started and nothing said so.
    -> ``test_acquire_is_called_with_core_signature_keywords``
  * sshim dialled with ITS OWN generated identity, while core had provisioned a DIFFERENT one on the
    container. -> ``test_the_plan_adopts_core_credential_not_our_own``
  * a raising acquire was swallowed. A refusal must ABORT the mint: silently minting a pivot that points
    at an unprovisioned host is the exact bug. -> ``test_a_raising_acquire_aborts_the_mint``

Only ``None`` (no docker on this host) degrades to Phase 1.
"""
from __future__ import annotations

import pytest

from sshim import blueprint as BP
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


def _handle(**over):
    """What core's `exclusive_container_acquire` really returns (see app/ephemeral_relay.py)."""
    d = {
        "handle_id": "h-1",
        "container": "lotek-exclusive-sshim-relay-abc",
        "host": "relay.internal",
        "port": 22022,
        "ssh_user": "p-core33",
        "ssh_password": "core-generated-secret-value",
    }
    d.update(over)
    return d


def _mint(db, tmp_path, prof=None, acquire=None):
    return service.mint_session(
        db, prof or _prof(),
        owner_id=None, standalone=True,
        ttl_minutes=5, max_ttl=60,
        secret_key_path=tmp_path / "seal.key",
        acquire=acquire,
    )


# ── the call shape ──────────────────────────────────────────────────────────


def test_acquire_is_called_with_core_signature_keywords(session_factory, tmp_path):
    """Core's hook is keyword-only: (name, *, ttl_minutes, requested_port). Positional breaks it."""
    calls = []

    def acquire(name, *, ttl_minutes=None, requested_port=None):
        calls.append({"name": name, "ttl": ttl_minutes})
        return _handle()

    with session_factory() as db:
        _mint(db, tmp_path, acquire=acquire)
        assert calls == [{"name": "sshim-relay", "ttl": 5}]


def test_the_plan_dials_the_container(session_factory, tmp_path):
    with session_factory() as db:
        row, _ = _mint(db, tmp_path, acquire=lambda name, **kw: _handle())
        assert service.relay_mode(row.params) == service.RELAY_CONTAINER
        assert row.rendezvous_host == "relay.internal"
        assert (row.params or {}).get("rendezvous_port") == 22022
        anchor = service.plan_for(row, secret_key_path=tmp_path / "seal.key").anchor["posix"]
        assert "relay.internal" in anchor and "-p 22022" in anchor
        assert "vps.example" not in anchor


def test_the_plan_adopts_core_credential_not_our_own(session_factory, tmp_path):
    """THE BUG: sshim used to dial with the identity it generated, which the container never had."""
    with session_factory() as db:
        row, secret = _mint(db, tmp_path, acquire=lambda name, **kw: _handle())
        assert row.ssh_user == "p-core33", "the stored user must be core's, not ours"
        assert secret == "core-generated-secret-value", "the mint must return core's password"
        anchor = service.plan_for(row, secret_key_path=tmp_path / "seal.key").anchor["posix"]
        assert "p-core33@" in anchor, "the rendered command must dial as the provisioned user"


def test_the_handle_is_recorded_for_release(session_factory, tmp_path):
    with session_factory() as db:
        row, _ = _mint(db, tmp_path, acquire=lambda name, **kw: _handle())
        assert service.relay_handle(row.params) == "h-1"


# ── failure behaviour (the half that shipped broken) ────────────────────────


def test_a_raising_acquire_aborts_the_mint(session_factory, tmp_path):
    """A refusal must surface. Silently degrading mints a pivot pointed at an unprovisioned host."""
    def boom(name, **kw):
        raise RuntimeError("no public port window is configured")

    with session_factory() as db:
        with pytest.raises(service.Invalid, match="relay container could not be started"):
            _mint(db, tmp_path, acquire=boom)


def test_an_answer_with_no_address_aborts_rather_than_minting_a_dead_pivot(session_factory, tmp_path):
    with session_factory() as db:
        with pytest.raises(service.Invalid, match="no usable address"):
            _mint(db, tmp_path, acquire=lambda name, **kw: {"handle_id": "h", "nonsense": True})


def test_none_means_no_docker_and_degrades_to_phase_one(session_factory, tmp_path):
    """The ONLY silent path: the host has no seam at all."""
    with session_factory() as db:
        row, _ = _mint(db, tmp_path, acquire=lambda name, **kw: None)
        assert service.relay_mode(row.params) == service.RELAY_EXTERNAL
        assert row.rendezvous_host == "vps.example"
        assert service.relay_handle(row.params) is None


def test_without_the_hook_the_plan_is_phase_one(session_factory, tmp_path):
    with session_factory() as db:
        row, _ = _mint(db, tmp_path, acquire=None)
        assert service.relay_mode(row.params) == service.RELAY_EXTERNAL
        rendered = service.plan_for(row, secret_key_path=tmp_path / "seal.key")
        assert "vps.example" in rendered.anchor["posix"]


def test_direct_topology_never_acquires(session_factory, tmp_path):
    """Direct talks to the anchor itself — nothing for a relay container to host."""
    calls = []
    with session_factory() as db:
        _mint(db, tmp_path,
              prof=_prof(topology="direct_socks", rendezvous_host=None, anchor_host="a.example"),
              acquire=lambda name, **kw: calls.append(name))
        assert calls == []


# ── teardown ────────────────────────────────────────────────────────────────


@pytest.fixture
def client_with_hooks(tmp_path):
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

    def acquire(name, **kw):
        acquired.append({"name": name, **kw})
        return _handle(handle_id="h-kill")

    def release(handle):
        released.append(handle)

    saved = (BP.exclusive_container_acquire, BP.exclusive_container_release)
    BP.exclusive_container_acquire = lambda: acquire
    BP.exclusive_container_release = lambda: release
    try:
        with app.test_client() as c:
            yield c, acquired, released
    finally:
        BP.exclusive_container_acquire, BP.exclusive_container_release = saved


def _mint_through_client(client):
    import re
    resp = client.post("/sshim/mint", data={
        "profile_id": "", "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": "22",
        "rendezvous_user": "sshim", "ttl_minutes": "5",
    })
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:400]
    return re.search(r"/sshim/sessions/([0-9a-f-]{36})", resp.get_data(as_text=True)).group(1)


def test_kill_releases_the_recorded_handle(client_with_hooks):
    client, acquired, released = client_with_hooks
    sid = _mint_through_client(client)
    assert acquired and acquired[0]["name"] == "sshim-relay"
    assert client.post(f"/sshim/sessions/{sid}/kill", data={"confirm": "1"}).status_code in (302, 303)
    assert released == ["h-kill"], "the container must be given back exactly once"


def test_kill_succeeds_even_when_release_raises(client_with_hooks):
    client, _acquired, _released = client_with_hooks
    sid = _mint_through_client(client)

    def boom(handle):
        raise RuntimeError("host already reaped it")

    BP.exclusive_container_release = lambda: boom
    # a teardown the host already performed must not block the operator's kill
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


def test_every_mint_call_site_passes_the_acquire_hook():
    """Structural guard for the gap the integration run found.

    The dashboard form was wired to the seam and the MACHINE api was not, so an agent-minted pivot
    silently took the Phase-1 fallback while a UI-minted one got a container. Both callers must pass
    `acquire`; this asserts it at the source so a new call site cannot forget.
    """
    import ast
    from pathlib import Path

    missing = []
    for name in ("blueprint.py", "api_pat.py"):
        src = (Path(__file__).resolve().parents[1] / "sshim" / name).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "mint_session"):
                keywords = {k.arg for k in node.keywords}
                if "acquire" not in keywords:
                    missing.append(f"{name}:{node.lineno}")
    assert not missing, f"mint_session call sites without acquire=<hook>: {missing}"
