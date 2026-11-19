"""Bulk kill + the running-pivots data table (#379).

The behaviours pinned here are the ones that fail without the work and whose failure mode is dangerous:

* two selected pivots -> BOTH killed and BOTH containers released (a bulk kill that silently skips one
  leaves a live pivot the operator believes is gone);
* one invalid/unknown id mixed in -> the valid one still dies and the invalid one is REPORTED, never
  swallowed;
* selection is keyed by session id, so an id is acted on even when its row is not in the current list
  window — the server never learns about the operator's filter, and must not require the row to be
  visible (the client half of that rule is the `draw.dt` reapply drift guard at the bottom);
* the audit entry stays PER PIVOT, not one row for the batch;
* the machine twin returns the same per-id shape;
* the table carries the documented `data-dt` attribute contract (core's `data-table.js` reads it) with
  the lifecycle/status and ISO-timestamp sort keys.

The Flask app is a stub with the host hooks injected in `extras`; the MOUNT itself is lotek's suite.
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import create_engine

import sshim
from sshim import api_pat as AP
from sshim import blueprint as BP
from sshim import models, plan, service

_PKG = Path(__file__).resolve().parent.parent


def _handle(n: int) -> dict:
    return {
        "handle_id": f"h-{n}",
        "container": f"lotek-exclusive-sshim-relay-{n}",
        "host": "relay.internal",
        "port": 22022,
        "ssh_user": f"p-core{n}",
        "ssh_password": f"core-generated-secret-{n}",
    }


@pytest.fixture
def env(tmp_path):
    """A stub mounted app + a recording container seam + a recording audit sink."""
    app = Flask(__name__)
    app.secret_key = "test"  # noqa: S105 - throwaway test app
    engine = create_engine("sqlite:///:memory:")
    cfg = sshim.register(app, engine, instance_path=str(tmp_path / "inst"))

    acquired: list[dict] = []
    released: list[str] = []
    audited: list[dict] = []

    def acquire(name, **kw):
        acquired.append({"name": name, **kw})
        return _handle(len(acquired))

    def release(handle):
        released.append(handle)

    def audit(db, action, *, subject_type=None, subject_id=None, before=None, after=None):
        audited.append({
            "action": action,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "before": before,
            "after": after,
        })

    # The machine API's PAT trio: a mounted operator, no auth work in the stub (the real gate is core's).
    from types import SimpleNamespace

    owner = uuid.uuid7()
    cfg.extras.update({
        "pat_authenticate": lambda: None,
        "pat_actor": lambda: SimpleNamespace(id=owner, username="op", role="operator"),
        "require_pat_scope": lambda scope: (lambda fn: fn),
        "audit": audit,
    })

    # Both surfaces resolve the seam through their own module namespace, and the test must wire both
    # or the machine mint silently takes the Phase-1 fallback (the exact bug test_relay_container pins).
    saved = (
        BP.exclusive_container_acquire, BP.exclusive_container_release,
        AP.exclusive_container_acquire, AP.exclusive_container_release,
    )
    BP.exclusive_container_acquire = AP.exclusive_container_acquire = lambda: acquire
    BP.exclusive_container_release = AP.exclusive_container_release = lambda: release
    try:
        with app.test_client() as client:
            yield SimpleNamespace(
                client=client, app=app, cfg=cfg, owner=owner,
                acquired=acquired, released=released, audited=audited,
            )
    finally:
        (
            BP.exclusive_container_acquire, BP.exclusive_container_release,
            AP.exclusive_container_acquire, AP.exclusive_container_release,
        ) = saved


def _mint(env, name="pivot") -> str:
    resp = env.client.post("/sshim/mint", data={
        "profile_id": "", "name": name, "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": "22022",
        "rendezvous_user": "", "ttl_minutes": "30",
    })
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)[:600]
    found = re.search(r"/sshim/sessions/([0-9a-f-]{36})", resp.get_data(as_text=True))
    assert found, resp.get_data(as_text=True)[:600]
    return found.group(1)


def _session(env, sid: str):
    with env.app.app_context(), env.cfg.session_factory() as db:
        return db.get(models.Session, uuid.UUID(sid))


def _bulk(env, ids, *, confirm=True):
    data = {"ids": list(ids)}
    if confirm:
        data["confirm"] = "1"
    return env.client.post("/sshim/sessions/bulk-kill", data=data)


# ── the whole point ─────────────────────────────────────────────────────────


def test_two_selected_pivots_are_both_killed_and_both_containers_released(env):
    first, second = _mint(env, "a"), _mint(env, "b")
    assert len(env.acquired) == 2

    resp = _bulk(env, [first, second])
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]

    assert _session(env, first).status == plan.STATUS_KILLED
    assert _session(env, second).status == plan.STATUS_KILLED
    assert env.released == ["h-1", "h-2"], "both containers must be given back"
    html = resp.get_data(as_text=True)
    assert "Killed 2 of 2" in html


def test_an_invalid_id_does_not_stop_the_valid_one_and_is_reported(env):
    valid = _mint(env)
    bogus = "not-a-uuid"
    unknown = str(uuid.uuid7())  # well-formed but no such row

    resp = _bulk(env, [valid, bogus, unknown])
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]

    assert _session(env, valid).status == plan.STATUS_KILLED, "the valid id must still be killed"
    assert env.released == ["h-1"]
    html = resp.get_data(as_text=True)
    assert "Killed 1 of 3" in html
    assert bogus in html and unknown in html
    assert "not found" in html


def test_the_confirmation_lists_the_targets_and_kills_nothing(env):
    sid = _mint(env, "prod-jump")
    resp = _bulk(env, [sid], confirm=False)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # profile + rendezvous + port, the three things the operator is approving
    assert "prod-jump" in html
    # the mint adopted the container's host/port, so THAT is what the confirmation must name
    assert "relay.internal" in html
    assert "22022" in html
    assert "Yes, kill 1 pivot" in html
    assert _session(env, sid).status == plan.STATUS_ACTIVE
    assert env.released == []


def test_every_kill_keeps_a_per_pivot_audit_entry(env):
    first, second = _mint(env, "a"), _mint(env, "b")
    env.audited.clear()
    _bulk(env, [first, second])
    kills = [a for a in env.audited if a["action"] == "ext:sshim:kill_session"]
    assert [a["subject_id"] for a in kills] == [first, second], "one audit row PER PIVOT, never one batch"


# ── hidden selection: the server acts on the ID, not on what is on screen ───


def test_a_selection_is_acted_on_even_when_its_row_is_outside_the_list_window(env):
    """The client half of "survives filtering/paging" is `draw.dt` reapply (drift guard below); the
    server half is that a posted id is acted on regardless of whether the list currently renders it.

    A selection made before the row aged off the list VIEW must still die — killing is by id, and the
    server never learns about the operator's filter."""
    sid = _mint(env)
    with env.app.app_context(), env.cfg.session_factory() as db:
        row = db.get(models.Session, uuid.UUID(sid))
        row.expires_at = service.now_utc() - plan.LISTABLE_GRACE - timedelta(minutes=1)
        db.commit()
    assert sid not in env.client.get("/sshim/").get_data(as_text=True), "row must be off the list view"

    assert _bulk(env, [sid]).status_code == 200
    assert _session(env, sid).status == plan.STATUS_KILLED


# ── the machine twin ────────────────────────────────────────────────────────


def test_the_machine_bulk_kill_returns_a_per_id_result(env):
    minted = env.client.post("/sshim/machine/sessions", json={
        "name": "agent", "topology": "relay_reverse_socks",
        "rendezvous_host": "vps.example", "rendezvous_port": 22022, "ttl_minutes": 30,
    })
    assert minted.status_code == 201, minted.get_data(as_text=True)[:600]
    sid = minted.get_json()["session"]["id"]
    env.released.clear()

    resp = env.client.post("/sshim/machine/sessions/bulk-kill", json={"ids": [sid, str(uuid.uuid7())]})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:600]
    body = resp.get_json()
    assert body["killed"] == 1 and body["failed"] == 1
    assert [r["status"] for r in body["results"]] == ["killed", "not_found"]
    assert env.released == ["h-1"], "an agent-killed pivot must reap exactly like a UI-killed one"


# ── the table contract ─────────────────────────────────────────────────────


def test_the_table_carries_the_documented_data_table_contract(env):
    sid = _mint(env)
    html = env.client.get("/sshim/").get_data(as_text=True)

    assert 'id="pivot-table"' in html and "data-dt" in html
    assert 'data-dt-filter="pivot-table"' in html, "the global filter box must target the table"
    assert "data-dt-colfilter" in html and "data-dt-empty=" in html
    # every data column is sortable; SOCKS is numeric
    assert html.count("<th data-sort") >= 8, "Profile..Kill at must all be sortable"
    assert 'data-sort-type="num"' in html
    # lifecycle order, not alphabetical: active -> expired -> killed
    assert 'data-sort-value="0"' in html, "an active row must carry status rank 0"
    # Expires/Kill at sort on the ISO timestamp, not the rendered clock
    row = _session(env, sid)
    assert f'data-sort-value="{row.expires_at.isoformat()}"' in html
    # multi-select surface
    assert 'id="pivot-select-all"' in html
    assert 'class="pivot-select" name="ids"' in html
    assert "0 selected" in html and "disabled" in html


def test_status_rank_is_the_lifecycle_order():
    assert [plan.status_rank(s) for s in ("active", "expired", "killed")] == [0, 1, 2]
    assert plan.status_rank("something-new") == 3  # last, never a KeyError that breaks the list


def test_the_selection_script_reapplies_on_draw_and_never_clears_hidden_selections():
    """Drift guard for the half pytest cannot run: selection is a Set of ids, re-applied on
    `draw.dt`, and nothing clears it on filter/page. A future edit that reads checked boxes off screen
    (`selected.clear()`, no `draw.dt`) would silently drop a hidden selection — exactly what the issue
    forbids."""
    source = (_PKG / "sshim" / "templates" / "sshim" / "sessions.html").read_text()
    assert "draw.dt" in source, "DataTables must reapply the selection after a filter/page draw"
    assert "selected.clear()" not in source
    assert "new Set()" in source and "selected.add(" in source
    assert 'name = "ids"' in source or 'input.name = "ids"' in source
