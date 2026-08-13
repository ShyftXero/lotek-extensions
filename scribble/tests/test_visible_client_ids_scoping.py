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


def test_sql_path_works_with_uuid_client_ids(app, stub_host, session_factory):
    """The shape PRODUCTION actually uses. Every other test here uses int client ids (standalone
    Scribble's own table), but a mounted v2 host's client PKs are UUIDv7, and `Engagement.client_id` is
    `SoftHostId` — TEXT-backed, with `process_bind_param` stringifying on the way in.

    That makes `.in_({uuid, …})` depend on the type's bind processor being applied to each element of the
    expanding IN. If it were not, the query would return NOTHING — no error, just an empty list, which a
    scoping helper renders as "you may see no engagements". A silently empty result is exactly the
    failure this asserts against, and it is why the assertion is on the ROW, not on "it didn't raise".
    """
    import uuid as _uuid

    held = _uuid.UUID("0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
    not_held = _uuid.UUID("0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a99")
    with session_factory() as db:
        db.add(fm.Engagement(name="UUID ours", client_id=held))
        db.add(fm.Engagement(name="UUID theirs", client_id=not_held))
        db.commit()

    stub_host.current_user = StubUser(id=63, username="uuid-member", role=_StubRole("operator"))
    _wire_set_hook(app, {held})

    with app.test_request_context(), session_factory() as db:
        rows = visible_engagements(db, select(fm.Engagement), stub_host.current_user)
    assert [e.name for e in rows] == ["UUID ours"], (
        "a UUID client id did not survive the IN bind — the SQL path would silently show nothing"
    )


def test_client_less_engagement_matches_the_predicate_on_both_paths(app, stub_host, session_factory):
    """`IN (…)` never matches NULL — so a client-less engagement would be invisible on the SQL path
    whatever the host thinks, while the predicate path shows it to anyone the host answers True for.

    Raised by Copilot on the first revision of this PR, and it is a real divergence rather than a
    theoretical one: the real lotek host answers False for a NULL client (v2 has no admin bypass, so
    nobody sees one), but Scribble's own test host answers True for an ADMIN — it checks the role before
    it ever looks at `client_id`. So the two paths disagreed for exactly the actor most likely to be
    looking at the dashboard, and `test_both_paths_agree` missed it because no fixture had a NULL client.

    Both directions are asserted, because a fix that simply always included NULL rows would be just as
    wrong in the other direction.
    """
    _clients_and_engagements(session_factory)
    with session_factory() as db:
        db.add(fm.Engagement(name="No client", client_id=None))
        db.commit()

    # Admin: the stub host grants a NULL client -> visible on BOTH paths.
    stub_host.current_user = StubUser(id=1, username="admin", role=_StubRole("admin"))
    with app.test_request_context(), session_factory() as db:
        fallback = visible_engagements(db, select(fm.Engagement), stub_host.current_user)
    _wire_set_hook(app, {ACME, OTHER_CLIENT})
    with app.test_request_context(), session_factory() as db:
        sql = visible_engagements(db, select(fm.Engagement), stub_host.current_user)
    assert "No client" in [e.name for e in fallback]
    assert sorted(e.name for e in fallback) == sorted(e.name for e in sql)

    # Non-admin member: the stub denies a NULL client -> hidden on BOTH paths.
    _member(stub_host)
    _wire_set_hook(app, {ACME})
    with app.test_request_context(), session_factory() as db:
        sql_member = visible_engagements(db, select(fm.Engagement), stub_host.current_user)
    assert [e.name for e in sql_member] == ["Ours Q3"]


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
