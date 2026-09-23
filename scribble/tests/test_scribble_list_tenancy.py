"""Tenancy on the routes with NO engagement id: the Report Boards list, Import-to-Scribble, and the
client field on edit.

The blueprint-wide gate (`scribble/authz.py::register_gate`) resolves an engagement from the request's
view args, so a route that names no engagement passes straight through — correctly, by its own design.
`tests/test_scribble_tenancy_gate.py` therefore lists `scribble.dashboard`, `scribble.engagements` and
`scribble.import_board` in `_NON_SCOPED_ENDPOINTS`: they carry no engagement id because they enumerate or
import core engagements, not scribble ones.

Two tenancy shapes an id-driven gate structurally cannot cover live here:

* **the unscoped dashboard** — the stat tiles were global `SELECT count(*)`s over every tenant's boards.
  Now filtered through `authz.filter_visible_engagements`.
* **the client field on edit** — `client_id` arrives in the FORM BODY on edit, so it is the tenancy
  decision itself rather than something to check a row against. Unchecked, editing MOVEs a board you hold
  onto a client you do not (or off yours onto theirs).

The Report Boards LIST is now a VIEW over `host.engagement_summaries` — the viewer's CORE engagements,
scoped by core. The ext renders exactly what the host returns; that list's tenancy is proven MOUNTED in
lotek's `tests/test_scribble_ui_mounted.py`, not restated here against a stub. The create-tenancy that
used to live on the create form's client field now lives on Import-to-Scribble (the one create path):
a board is created only for a core engagement the actor OPERATES (`host.can_operate_on`), with the client
DERIVED from the summary — never a form field — so a board can't be planted under a foreign client.

Standalone Scribble (no host bundle) has no core engagements to proxy, so its Report Boards list is empty
and its standalone create form is retired — proven at the bottom.
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


def _engagement(session_factory, *, client_id, name) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name=name, client_id=client_id)
        db.add(eng)
        db.commit()
        return eng.id


def _member(stub_host) -> None:
    """A real user with a real grant — under ONE of the two clients. Not an admin: the stub grants an
    admin everything, which would prove nothing about the filtering."""
    stub_host.current_user = StubUser(id=62, username="member", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME}


def _summary(core_id, name, client_id, client_name) -> dict:
    return {"id": core_id, "name": name, "client_id": client_id, "client_name": client_name}


# ── the dashboard: still a scribble-board view, still scoped ────────────────────────────────────────────


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
            db.add(fm.BoardFinding.from_template(tmpl, engagement_id=eid, order_index=0))
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


# ── the Report Boards list: a VIEW over the viewer's CORE engagements ──────────────────────────────────
# The list proxies host.engagement_summaries (core-scoped there) rather than listing scribble boards. So
# the LIST TENANCY (which engagements appear) is core's job, proven mounted in lotek's
# tests/test_scribble_ui_mounted.py; here we prove the ext renders exactly what the host returns, and the
# create-tenancy that used to live on the form's client field now lives on Import (below).


def test_report_boards_list_shows_the_core_engagement_and_offers_import(client, stub_host, session_factory):
    _clients(session_factory)
    core = uuid.uuid7()
    stub_host.engagement_summaries_value = [_summary(core, "Ours Q3", ACME, "Acme Corp")]
    _member(stub_host)

    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Ours Q3" in body               # the core engagement is listed by name
    assert "Import to Scribble" in body     # no board yet -> Import offered
    assert str(core) in body                # the import form carries the core id


def test_report_boards_list_opens_an_existing_board(client, stub_host, session_factory):
    _clients(session_factory)
    core = uuid.uuid7()
    with session_factory() as db:
        board = fm.ReportBoard(name="Ours Q3", client_id=ACME, core_engagement_id=core)
        db.add(board)
        db.commit()
        board_id = board.id
    stub_host.engagement_summaries_value = [_summary(core, "Ours Q3", ACME, "Acme Corp")]
    _member(stub_host)

    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert f"/scribble/engagements/{board_id}" in body   # Open-board link
    assert "Import to Scribble" not in body               # already imported


# ── Import-to-Scribble: the ONE create path (the standalone create form is retired) ────────────────────


def test_import_refuses_an_engagement_the_actor_cannot_operate(client, stub_host, session_factory):
    """The create-tenancy that used to be the form's client field is now the import gate: a board is
    created only for a core engagement the actor OPERATES. can_operate_on False -> 404 (no oracle),
    no board."""
    core = uuid.uuid7()
    stub_host.can_operate_value = False
    _member(stub_host)

    resp = client.post(f"{UI}/engagements/import", data={"core_engagement_id": str(core)})
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.query(fm.ReportBoard).count() == 0


def test_import_creates_a_board_linked_to_the_core_engagement(client, stub_host, session_factory):
    """Name + client are DERIVED from the core engagement summary, never the form, so a board cannot be
    planted under a foreign client. can_operate_on True -> board created and linked."""
    _clients(session_factory)
    core = uuid.uuid7()
    stub_host.can_operate_value = True
    stub_host.engagement_summaries_value = [_summary(core, "Ours Q3", ACME, "Acme Corp")]
    _member(stub_host)

    resp = client.post(f"{UI}/engagements/import", data={"core_engagement_id": str(core)})
    assert resp.status_code == 302
    with session_factory() as db:
        boards = db.query(fm.ReportBoard).all()
        assert len(boards) == 1
        assert boards[0].core_engagement_id == core and boards[0].client_id == ACME


def test_import_is_idempotent(client, stub_host, session_factory):
    """A second import opens the existing board rather than making a second one (the 1:1 rule)."""
    _clients(session_factory)
    core = uuid.uuid7()
    stub_host.can_operate_value = True
    stub_host.engagement_summaries_value = [_summary(core, "Ours Q3", ACME, "Acme Corp")]
    _member(stub_host)

    first = client.post(f"{UI}/engagements/import", data={"core_engagement_id": str(core)})
    second = client.post(f"{UI}/engagements/import", data={"core_engagement_id": str(core)})
    assert first.status_code == 302 and second.status_code == 302
    assert first.headers["Location"] == second.headers["Location"]
    with session_factory() as db:
        assert db.query(fm.ReportBoard).count() == 1


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
        assert db.get(fm.ReportBoard, eid).client_id == ACME  # untouched


def test_edit_cannot_strip_the_client_when_mounted(client, stub_host, session_factory):
    """Clearing the client is the same move by another route: the engagement becomes readable by nobody
    (`can_view_client(None, …)` is False), i.e. deleted from everyone's view without a delete."""
    _clients(session_factory)
    eid = _engagement(session_factory, client_id=ACME, name="Ours Q3")
    _member(stub_host)

    resp = client.post(f"{UI}/engagements/{eid}/edit", data={"name": "Ours Q3", "client_id": ""})
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eid).client_id == ACME


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
        eng = db.get(fm.ReportBoard, eid)
        assert eng.name == "Ours Q3 (renamed)" and eng.client_id == ACME


# ── standalone: no host bundle, so no core engagements to proxy and no create form ───────────────────


def test_standalone_report_boards_list_is_empty(client, session_factory):
    """No `stub_host` fixture: `host.engagement_summaries` has no hook and returns []. Standalone
    Scribble has no core engagements to proxy, so the Report Boards list offers nothing to import —
    boards are created only by importing a core engagement, which only exists mounted."""
    _clients(session_factory)
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    assert "Import to Scribble" not in body


def test_standalone_create_form_is_retired(client):
    """engagement_new is now a redirect to the Report Boards list — standalone create is gone, so a board
    can never be an orphan with no core engagement behind it."""
    resp = client.get(f"{UI}/engagements/new")
    assert resp.status_code in (302, 303)
    assert "/scribble/engagements" in resp.headers.get("Location", "")
