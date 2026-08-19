"""Tenancy on the routes with NO engagement id: the lists, and the client field on create/edit.

The blueprint-wide gate (`scribble/authz.py::register_gate`) resolves an engagement from the request's
view args, so a route that names no engagement passes straight through — correctly, by its own design.
`tests/test_scribble_tenancy_gate.py` therefore listed `scribble.dashboard`, `scribble.engagements` and
`scribble.engagement_new` in `_NON_SCOPED_ENDPOINTS`. That classification was accurate and the conclusion
drawn from it was not: those three routes carry no engagement id because they enumerate ALL of them.

So this file covers the two shapes of the same tenancy defect that an id-driven gate structurally cannot:

* **the unscoped list** — the dashboard and the engagement index rendered every tenant's engagement names
  and client names, with no id to guess at all. Now filtered through `authz.filter_visible_engagements`.
* **the client field** — `client_id` (and `new_client_name`) arrive in the FORM BODY on create and edit,
  so they are the tenancy decision itself rather than something to check a row against. Unchecked, that
  is create-under-someone-else's-client and, on edit, MOVE an engagement you hold onto a client you do
  not (or off yours onto theirs).

Standalone Scribble (no host bundle) keeps its old behaviour verbatim — proven at the bottom, because a
tenancy fix that quietly breaks the unmounted product is a different bug, not a fix.

Red → green: with `filter_visible_engagements` dropped from `blueprint.dashboard`/`engagement_ui
.engagements`, the two list tests fail (the foreign engagement's name is present); with
`engagement_ui._resolve_client`'s three rules removed, all five create/edit DENY tests fail (302 + a
persisted row instead of 404/400 + nothing).
"""

from __future__ import annotations

import uuid

import scribble.models as fm
from tests.conftest import StubUser, _StubRole

UI = "/scribble"

# Scribble's own client PK is a UUIDv7 since lotek#335, and ACME doubles as the HOST client
# id the stub grants on -- so both halves have to move together or the grant stops matching.
ACME = uuid.uuid7()          # the client the actor holds a grant under
OTHER_CLIENT = uuid.uuid7()  # a client it does not


def _clients(session_factory) -> None:
    with session_factory() as db:
        db.add(fm.Client(id=ACME, name="Acme Corp"))
        db.add(fm.Client(id=OTHER_CLIENT, name="Umbrella Corp"))
        db.commit()


def _engagement(session_factory, *, client_id, name) -> int:
    with session_factory() as db:
        eng = fm.Engagement(name=name, client_id=client_id)
        db.add(eng)
        db.commit()
        return eng.id


def _member(stub_host) -> None:
    """A real user with a real grant — under ONE of the two clients. Not an admin: the stub grants an
    admin everything, which would prove nothing about the filtering."""
    stub_host.current_user = StubUser(id=62, username="member", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME}


# ── the unscoped lists ───────────────────────────────────────────────────────────────────────────────


def test_dashboard_lists_only_the_viewers_clients(client, stub_host, session_factory):
    _clients(session_factory)
    _engagement(session_factory, client_id=ACME, name="Ours Q3")
    _engagement(session_factory, client_id=OTHER_CLIENT, name="Theirs Q3")
    _member(stub_host)

    body = client.get(f"{UI}/").get_data(as_text=True)
    assert "Ours Q3" in body
    assert "Theirs Q3" not in body
    assert "Umbrella Corp" not in body  # nor the other tenant's client name via client_names()


def test_dashboard_counts_are_scoped_too(client, stub_host, session_factory):
    """The stat tiles were global `SELECT count(*)`s. A count is a smaller leak than a name and it is
    still one — and a tile that disagrees with the list under it is how a scoping fix rots."""
    _clients(session_factory)
    ours = _engagement(session_factory, client_id=ACME, name="Ours Q3")
    theirs = _engagement(session_factory, client_id=OTHER_CLIENT, name="Theirs Q3")
    with session_factory() as db:
        tmpl = fm.VulnerabilityTemplate(name="T", content_json={}, content_html={})
        db.add(tmpl)
        db.commit()
        for eid in (ours, theirs):
            db.add(fm.EngagementFinding.from_template(tmpl, engagement_id=eid, order_index=0))
        db.commit()
    _member(stub_host)

    body = client.get(f"{UI}/").get_data(as_text=True)
    # One engagement, one finding, one client — not two of each.
    engagements_tile = body.split('<div class="label">Engagements</div>')[1].split("</div>")[0]
    findings_tile = body.split('<div class="label">Findings</div>')[1].split("</div>")[0]
    clients_tile = body.split('<div class="label">Clients</div>')[1].split("</div>")[0]
    assert engagements_tile.endswith(">1")
    assert findings_tile.endswith(">1")
    assert clients_tile.endswith(">1")


def test_engagement_list_shows_only_the_viewers_clients(client, stub_host, session_factory):
    _clients(session_factory)
    _engagement(session_factory, client_id=ACME, name="Ours Q3")
    _engagement(session_factory, client_id=OTHER_CLIENT, name="Theirs Q3")
    _member(stub_host)

    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Ours Q3" in body
    assert "Theirs Q3" not in body


def test_client_picker_shows_only_the_viewers_clients(client, stub_host, session_factory):
    """The create/edit form's `<select>` rendered the mounted host's entire client table. The client
    roster is tenancy data in its own right."""
    _clients(session_factory)
    _member(stub_host)

    body = client.get(f"{UI}/engagements/new").get_data(as_text=True)
    assert "Acme Corp" in body
    assert "Umbrella Corp" not in body


# ── the client field on create ───────────────────────────────────────────────────────────────────────


def _engagement_count(session_factory) -> int:
    with session_factory() as db:
        return len(db.query(fm.Engagement).all())


def test_create_denies_a_client_the_actor_cannot_view(client, stub_host, session_factory):
    _clients(session_factory)
    _member(stub_host)

    resp = client.post(
        f"{UI}/engagements/new", data={"name": "Planted", "client_id": str(OTHER_CLIENT)}
    )
    assert resp.status_code == 404  # not 403 — don't confirm the client id is real
    assert _engagement_count(session_factory) == 0


def test_create_requires_a_client_when_mounted(client, stub_host, session_factory):
    """`can_view_client(None, actor)` is False by the host's contract, so a client-less engagement 404s
    for everyone including its creator — a redirect to a board nobody can open."""
    _clients(session_factory)
    _member(stub_host)

    resp = client.post(f"{UI}/engagements/new", data={"name": "No client"})
    assert resp.status_code == 400
    assert "client is required" in resp.get_data(as_text=True)
    assert _engagement_count(session_factory) == 0


def test_create_refuses_to_make_a_client_in_the_hosts_table(client, stub_host, session_factory):
    """`client_model()` is the HOST's client table when mounted, so "or a new client name" was creating
    rows in lotek's own tenancy data from a Scribble form — under no membership of the creator's."""
    _clients(session_factory)
    _member(stub_host)

    resp = client.post(
        f"{UI}/engagements/new", data={"name": "New client", "new_client_name": "Fresh Corp"}
    )
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.query(fm.Client).filter_by(name="Fresh Corp").first() is None
    assert _engagement_count(session_factory) == 0


def test_create_allows_a_client_the_actor_holds(client, stub_host, session_factory):
    _clients(session_factory)
    _member(stub_host)

    resp = client.post(f"{UI}/engagements/new", data={"name": "Ours", "client_id": str(ACME)})
    assert resp.status_code == 302
    with session_factory() as db:
        assert [e.client_id for e in db.query(fm.Engagement).all()] == [ACME]


# ── the client field on edit (the MOVE case) ─────────────────────────────────────────────────────────


def test_edit_cannot_move_an_engagement_to_a_foreign_client(client, stub_host, session_factory):
    """The gate proves the actor may touch this engagement AS IT IS. Where it may be moved TO is a
    second question, and it was not being asked: this hands an engagement to another tenant (or, run the
    other way, takes one from them)."""
    _clients(session_factory)
    eid = _engagement(session_factory, client_id=ACME, name="Ours Q3")
    _member(stub_host)

    resp = client.post(
        f"{UI}/engagements/{eid}/edit", data={"name": "Ours Q3", "client_id": str(OTHER_CLIENT)}
    )
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.get(fm.Engagement, eid).client_id == ACME  # untouched


def test_edit_cannot_strip_the_client_when_mounted(client, stub_host, session_factory):
    """Clearing the client is the same move by another route: the engagement becomes readable by nobody
    (`can_view_client(None, …)` is False), i.e. deleted from everyone's view without a delete."""
    _clients(session_factory)
    eid = _engagement(session_factory, client_id=ACME, name="Ours Q3")
    _member(stub_host)

    resp = client.post(f"{UI}/engagements/{eid}/edit", data={"name": "Ours Q3", "client_id": ""})
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.get(fm.Engagement, eid).client_id == ACME


def test_edit_within_the_actors_own_clients_still_works(client, stub_host, session_factory):
    """The companion positive: the refusals above must not have made a legitimate edit impossible."""
    _clients(session_factory)
    eid = _engagement(session_factory, client_id=ACME, name="Ours Q3")
    _member(stub_host)

    resp = client.post(
        f"{UI}/engagements/{eid}/edit", data={"name": "Ours Q3 (renamed)", "client_id": str(ACME)}
    )
    assert resp.status_code == 302
    with session_factory() as db:
        eng = db.get(fm.Engagement, eid)
        assert eng.name == "Ours Q3 (renamed)" and eng.client_id == ACME


# ── standalone: no host bundle, therefore no tenancy model to apply ─────────────────────────────────


def test_standalone_lists_everything_and_still_creates_clients(client, session_factory):
    """No `stub_host` fixture: `cfg.extras['host']` is absent. Every check above degrades to "allowed",
    exactly as `authorize_engagement_view` has always done — including the create form's
    create-a-client-by-name path, which is only a host-tenancy problem when there IS a host."""
    _clients(session_factory)
    _engagement(session_factory, client_id=ACME, name="Ours Q3")
    _engagement(session_factory, client_id=OTHER_CLIENT, name="Theirs Q3")

    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Ours Q3" in body and "Theirs Q3" in body

    resp = client.post(
        f"{UI}/engagements/new", data={"name": "Standalone", "new_client_name": "Fresh Corp"}
    )
    assert resp.status_code == 302
    with session_factory() as db:
        assert db.query(fm.Client).filter_by(name="Fresh Corp").first() is not None
