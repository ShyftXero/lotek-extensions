"""`authz.visible_engagements` — scope the list routes in SQL via the host's `visible_client_ids`.

The tenancy fix (ext#12) could only ask a per-client PREDICATE, so both list routes had to read every
engagement row and filter in Python — the accepted cost recorded in that branch's plan. The host now
exposes the SET (`extras['visible_client_ids']`), so the same answer arrives in one call and the
database returns only the rows the viewer may see.

The two things worth pinning are the ones that would quietly reopen the hole:

1. **An empty set is not "unscoped".** `None` (no host / older bundle) means "no set available, fall back
   to the predicate"; `frozenset()` means "this actor holds nothing" and must scope everything away. A
   truthiness check that treats both the same is the classic fail-open, so it is tested directly.
2. **Both paths agree.** With and without the hook, the same actor must see the same engagements —
   otherwise the SQL path is a second, drifting copy of the predicate, which is the defect this whole
   line of work exists to end.
"""

from __future__ import annotations

from sqlalchemy import select

import scribble.models as fm
from scribble.authz import host_visible_client_ids, visible_engagements
from tests.conftest import StubUser, _StubRole

UI = "/scribble"

ACME = 501
OTHER_CLIENT = 502


def _clients_and_engagements(session_factory) -> None:
    with session_factory() as db:
        db.add(fm.Client(id=ACME, name="Acme Corp"))
        db.add(fm.Client(id=OTHER_CLIENT, name="Umbrella Corp"))
        db.add(fm.Engagement(name="Ours Q3", client_id=ACME))
        db.add(fm.Engagement(name="Theirs Q3", client_id=OTHER_CLIENT))
        db.commit()


def _member(stub_host) -> None:
    stub_host.current_user = StubUser(id=62, username="member", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME}


def _wire_set_hook(app, ids) -> None:
    """Add the host's newer `visible_client_ids` hook to the mounted config, as `_inject_host` does."""
    app.extensions["scribble"].extras["visible_client_ids"] = lambda: frozenset(ids)


# ── the hook itself ─────────────────────────────────────────────────────────────────────────────────


def test_no_set_available_is_none_not_empty(app, stub_host):
    """The `stub_host` fixture wires the older bundle (no `visible_client_ids`), which must read as
    "fall back to the predicate", never as "this actor holds nothing"."""
    with app.test_request_context():
        assert host_visible_client_ids() is None


def test_standalone_has_no_set(app):
    with app.test_request_context():
        assert host_visible_client_ids() is None


def test_hook_value_is_returned_as_a_set(app, stub_host):
    _wire_set_hook(app, {ACME})
    with app.test_request_context():
        assert host_visible_client_ids() == frozenset({ACME})


# ── the SQL path ────────────────────────────────────────────────────────────────────────────────────


def test_sql_path_returns_only_the_actors_clients(app, stub_host, session_factory):
    _clients_and_engagements(session_factory)
    _member(stub_host)
    _wire_set_hook(app, {ACME})

    with app.test_request_context(), session_factory() as db:
        rows = visible_engagements(db, select(fm.Engagement), stub_host.current_user)
    assert [e.name for e in rows] == ["Ours Q3"]


def test_empty_set_scopes_everything_away(app, stub_host, session_factory):
    """The fail-open trap: `if not client_ids` must mean "sees nothing", not "skip scoping"."""
    _clients_and_engagements(session_factory)
    _member(stub_host)
    _wire_set_hook(app, set())

    with app.test_request_context(), session_factory() as db:
        rows = visible_engagements(db, select(fm.Engagement), stub_host.current_user)
    assert rows == []


def test_both_paths_agree(app, stub_host, session_factory):
    """SQL path and predicate fallback must produce the same list for the same actor."""
    _clients_and_engagements(session_factory)
    _member(stub_host)

    with app.test_request_context(), session_factory() as db:
        fallback = visible_engagements(db, select(fm.Engagement), stub_host.current_user)
    _wire_set_hook(app, {ACME})
    with app.test_request_context(), session_factory() as db:
        sql = visible_engagements(db, select(fm.Engagement), stub_host.current_user)

    assert [e.name for e in fallback] == [e.name for e in sql] == ["Ours Q3"]


# ── the routes that use it ──────────────────────────────────────────────────────────────────────────


def test_list_and_dashboard_scope_through_the_hook(client, app, stub_host, session_factory):
    """End to end through the real routes, with the hook wired — same expectations as the predicate-only
    coverage in `test_scribble_list_tenancy.py`, so the newer path cannot regress the older guarantee."""
    _clients_and_engagements(session_factory)
    _member(stub_host)
    _wire_set_hook(app, {ACME})

    listing = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Ours Q3" in listing and "Theirs Q3" not in listing

    dashboard = client.get(f"{UI}/").get_data(as_text=True)
    assert "Ours Q3" in dashboard and "Theirs Q3" not in dashboard
    assert "Umbrella Corp" not in dashboard
