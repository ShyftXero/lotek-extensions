"""The two settings scopes (lotek#485 / lotek-extensions#111).

ADMIN / per-install (`deliverable_footer`): declared in `lotek-extension.toml` `[[settings]]`, owned,
gated, stored and audited by the HOST; Vector only reads it through `deps.host_setting`.

USER / per-user (`hide_builtin_diagrams`): Vector's own `vector_user_prefs` row, reached through
Vector's own ⚙ cog. No admin gate, no audit, no host involvement — and, critically, no influence on
`blueprint.visible_diagrams_stmt()`, which is the IDOR guard.

The mounted half of the admin scope (that lotek really injects `extras["extension_setting"]`) is
proven on the lotek side; here the seam is the injected stub the conftest provides, which is what
every other host capability in this suite is tested against.
"""

from __future__ import annotations

import pathlib
import tomllib
import uuid

from conftest import FakeUser, login
from sqlalchemy import select

from vector.schema import blank_model

ALICE = FakeUser(uuid.UUID(int=1), "alice", "operator")
BOB = FakeUser(uuid.UUID(int=2), "bob", "operator")

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "lotek-extension.toml"


def _create(client, name="D"):
    return client.post("/vector/api/diagrams", json={"name": name, "model": blank_model(name)})


# ── the manifest declaration ───────────────────────────────────────────────────────────────────


def test_the_manifest_declares_the_admin_setting():
    data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    declared = {entry["key"]: entry for entry in data["settings"]}
    assert "deliverable_footer" in declared
    footer = declared["deliverable_footer"]
    assert footer["type"] == "str" and footer["default"] == ""
    assert footer.get("label") and footer.get("help")


def test_no_per_user_preference_leaks_into_the_manifest():
    """`[[settings]]` is the ADMIN scope. A per-user preference declared here would be rendered on the
    host's admin page and stored once for the whole install — i.e. silently promoted to a global."""
    data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    keys = {entry["key"] for entry in data["settings"]}
    assert "hide_builtin_diagrams" not in keys
    assert not any(k.startswith("my_") or k.startswith("user_") for k in keys)


# ── host_setting: the read seam ────────────────────────────────────────────────────────────────


def test_host_setting_reads_what_the_host_holds(app):
    from vector.deps import host_setting

    app.host_settings["deliverable_footer"] = "CONFIDENTIAL — Contoso"
    with app.test_request_context("/vector/"):
        assert host_setting("deliverable_footer", "") == "CONFIDENTIAL — Contoso"
        assert host_setting("never_declared", "fallback") == "fallback"


def test_host_setting_degrades_to_the_default_with_no_host(app):
    """Standalone Vector has no host bundle at all; a settings read must not be what breaks it."""
    from vector.deps import host_setting

    app.extensions["vector"].extras.pop("extension_setting", None)
    with app.test_request_context("/vector/"):
        assert host_setting("deliverable_footer", "") == ""


def test_host_setting_survives_a_throwing_host_hook(app):
    from vector.deps import host_setting

    def boom(key, default=None):
        raise RuntimeError("host exploded")

    app.extensions["vector"].extras["extension_setting"] = boom
    with app.test_request_context("/vector/"):
        assert host_setting("deliverable_footer", "sane") == "sane"


# ── the admin setting's read site: the exported deliverable ────────────────────────────────────


def test_the_footer_is_stamped_into_an_exported_deliverable(app, client):
    login(app, ALICE)
    app.host_settings["deliverable_footer"] = "CONFIDENTIAL — Contoso Security"
    did = _create(client, "Export me").get_json()["id"]
    html = client.get(f"/vector/diagrams/{did}/export.html").get_data(as_text=True)
    assert "CONFIDENTIAL — Contoso Security" in html
    assert "vap-deliverable-footer" in html


def test_an_unset_footer_renders_no_footer_element(app, client):
    login(app, ALICE)
    did = _create(client, "Plain").get_json()["id"]
    html = client.get(f"/vector/diagrams/{did}/export.html").get_data(as_text=True)
    assert "<footer" not in html


def test_the_footer_is_escaped_not_injected(app, client):
    """An admin form is still an input boundary: the footer must not become markup in a deliverable
    a client opens."""
    login(app, ALICE)
    app.host_settings["deliverable_footer"] = '</footer><script>alert(1)</script>'
    did = _create(client, "XSS").get_json()["id"]
    html = client.get(f"/vector/diagrams/{did}/export.html").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_unsaved_export_endpoint_stamps_it_too(app, client):
    """All three export paths must agree — a deliverable exported from the editor without saving is
    still a deliverable."""
    login(app, ALICE)
    app.host_settings["deliverable_footer"] = "CONFIDENTIAL"
    r = client.post("/vector/api/export.html", json={"model": blank_model("x"), "title": "x"})
    assert "CONFIDENTIAL" in r.get_data(as_text=True)


# ── the user scope: Vector's own cog ───────────────────────────────────────────────────────────


def test_the_cog_shows_when_there_is_a_host_identity(app, client):
    login(app, ALICE)
    body = client.get("/vector/").get_data(as_text=True)
    assert "data-vector-settings-cog" in body
    assert "/vector/settings" in body


def test_no_cog_and_no_page_without_a_host_identity(app, client):
    """Standalone / anonymous: there is no "my" to scope a preference to, so writing one would make
    one session's choice everyone's. No identity, no page."""
    login(app, None)
    assert "data-vector-settings-cog" not in client.get("/vector/").get_data(as_text=True)
    assert client.get("/vector/settings").status_code == 404
    assert client.post("/vector/settings", data={"hide_builtin_diagrams": "1"}).status_code == 404


def test_saving_the_preference_persists_it_for_that_user(app, client):
    login(app, ALICE)
    assert client.get("/vector/settings").status_code == 200
    r = client.post("/vector/settings", data={"hide_builtin_diagrams": "1"}, follow_redirects=True)
    assert r.status_code == 200
    body = client.get("/vector/settings").get_data(as_text=True)
    assert 'id="vec-hide-builtin"' in body and "checked" in body

    from vector.models import UserPref

    with app.extensions["vector"].session_factory() as db:
        rows = db.query(UserPref).all()
    assert len(rows) == 1
    assert rows[0].owner_id == ALICE.id and rows[0].hide_builtin_diagrams is True


def test_unticking_turns_it_back_off(app, client):
    """An HTML checkbox submits nothing when unticked — "absent" must mean off, or it can never be
    turned back off through the form."""
    login(app, ALICE)
    client.post("/vector/settings", data={"hide_builtin_diagrams": "1"})
    client.post("/vector/settings", data={})
    from vector.models import UserPref

    with app.extensions["vector"].session_factory() as db:
        assert db.query(UserPref).one().hide_builtin_diagrams is False


def test_the_preference_hides_builtins_from_only_that_users_list(make_app):
    app = make_app(seed=True)  # seeds the read-only builtin example
    client = app.test_client()

    # Grade on the builtin diagram's NAME, not on page byte-length: `len(after) < len(before)`
    # passes for any shorter page (a flash message disappearing would satisfy it), and
    # `len(bob) == len(before)` silently couples the assertion to two users rendering byte-identical
    # HTML. Ask the question the test is named for instead.
    from vector.models import Diagram

    with app.extensions["vector"].session_factory() as db:
        builtin_name = db.scalars(select(Diagram).where(Diagram.builtin.is_(True))).first().name

    login(app, ALICE)
    assert builtin_name in client.get("/vector/").get_data(as_text=True)
    client.post("/vector/settings", data={"hide_builtin_diagrams": "1"})
    # Assert the page RENDERED as well: "the name is absent" is also true of a 500 or a redirect,
    # which is the same shape as the byte-length assertion this replaced.
    after = client.get("/vector/")
    assert after.status_code == 200
    assert builtin_name not in after.get_data(as_text=True), \
        "alice's list should have lost the builtin row"

    login(app, BOB)
    assert builtin_name in client.get("/vector/").get_data(as_text=True), \
        "alice's preference must not touch bob's list"


def test_the_preference_never_hides_a_users_OWN_diagram(app, client):
    """The preference filters builtins only. If it ever became an access predicate, this is the shape
    that would break first."""
    login(app, ALICE)
    _create(client, "Mine-and-visible")
    client.post("/vector/settings", data={"hide_builtin_diagrams": "1"})
    assert "Mine-and-visible" in client.get("/vector/").get_data(as_text=True)


def test_the_preference_is_not_part_of_the_access_guard(make_app):
    """`visible_diagrams_stmt()` is the IDOR guard. Folding a preference into it is how a preference
    bug becomes a disclosure bug, so the guard must be provably blind to the preference."""
    app = make_app(seed=True)
    client = app.test_client()
    from vector.blueprint import visible_diagrams_stmt

    login(app, ALICE)
    client.post("/vector/settings", data={"hide_builtin_diagrams": "1"})
    with app.test_request_context("/vector/"):
        app.holder["actor"] = ALICE
        with app.extensions["vector"].session_factory() as db:
            visible = db.scalars(visible_diagrams_stmt()).all()
    assert any(d.builtin for d in visible), \
        "the access guard must still return the builtin — only the VIEW filters it"


def test_one_user_cannot_write_another_users_preference(app, client):
    """The form carries no owner field; the row is keyed off the session identity alone, so there is
    nothing for a caller to point at someone else's row."""
    login(app, ALICE)
    client.post("/vector/settings", data={"hide_builtin_diagrams": "1"})
    login(app, BOB)
    client.post("/vector/settings", data={"owner_id": str(ALICE.id)})

    from vector.models import UserPref

    with app.extensions["vector"].session_factory() as db:
        by_owner = {r.owner_id: r.hide_builtin_diagrams for r in db.query(UserPref).all()}
    assert by_owner == {ALICE.id: True, BOB.id: False}


# ── review-driven guards (2026-08-28 catch-up review) ──────────────────────────────────────────


def test_a_non_string_host_setting_cannot_break_the_export_it_decorates():
    """`host_setting` promises a settings lookup never breaks the export it decorates. That promise
    died one call later: `render_deliverable` did `(footer or "").strip()`, so a host returning a
    dict/int raised AttributeError and 500'd the CLIENT deliverable — not the settings page.

    `extras["extension_setting"]` is a generic host seam. lotek#485 happens to coerce a `type="str"`
    field to `str` before we see it, but Vector must not depend on the far side of a seam to hold the
    bound it needs.
    """
    from vector.render import render_deliverable

    class Boom:
        def __str__(self):  # a host value whose str() ITSELF reaches back into the far side
            raise RuntimeError("boom from the host setting")

    for hostile in ({"a": 1}, 7, ["x"], None, object(), Boom(), b"CONFIDENTIAL"):
        html = render_deliverable({}, title="t", footer=hostile)
        assert "<html" in html
        # ...and a non-string is DROPPED, never stamped into a client document. `str()` would have
        # rendered b'CONFIDENTIAL' and {'a': 1} verbatim into the deliverable.
        assert "CONFIDENTIAL" not in html and "a&#39;: 1" not in html


def test_the_footer_is_length_bounded_and_reserves_a_strip_rather_than_overlaying():
    from vector.render import render_deliverable

    html = render_deliverable({}, title="t", footer="A" * 5000)
    assert "A" * 201 not in html, "the footer must be truncated, not stamped in full"
    # Linkage, not layout: without a browser this can only prove the strip rule is EMITTED. It is
    # named accordingly. It discriminates — "padding-bottom" appears nowhere in the viewer's own CSS.
    assert "padding-bottom" in html
    # ...and is NOT emitted when there is no footer, which is what replaces the `:has()` selector.
    assert "padding-bottom" not in render_deliverable({}, title="t", footer="")


def test_a_double_clicked_save_does_not_500_on_the_unique_constraint(app, client, monkeypatch):
    """`owner_id` is UNIQUE and the first save was select-then-insert — a check-then-act. Two racing
    saves (a double-clicked Save; two gevent workers in prod) both read `None` and both insert, and
    the loser hits the constraint.

    The race is reproduced by its OBSERVABLE CONDITION, not by threads: the losing racer is exactly a
    request whose existence check said "no row" while a row does exist by the time it writes. So a
    real row is committed underneath, and the request's FIRST existence check is forced to miss it.
    Without the patch this reproduces nothing — the request's own read would simply find the row and
    update it, which is why the naive version of this test passed against the broken code.
    """
    from vector import blueprint as vb
    from vector.models import UserPref

    login(app, ALICE)
    uid = ALICE.id
    with app.extensions["vector"].session_factory() as db:
        db.add(UserPref(owner_id=uid, hide_builtin_diagrams=False))
        db.commit()

    real_prefs, calls = vb._prefs, []

    def stale_first_read(db, uid=None):
        calls.append(1)
        return None if len(calls) == 1 else real_prefs(db, uid)

    monkeypatch.setattr(vb, "_prefs", stale_first_read)

    resp = client.post("/vector/settings", data={"hide_builtin_diagrams": "1"})
    assert resp.status_code in (302, 303), "the losing racer 500'd instead of retrying"
    with app.extensions["vector"].session_factory() as db:
        rows = db.scalars(select(UserPref).where(UserPref.owner_id == uid)).all()
    assert len(rows) == 1, "the retry must not leave a duplicate row"
    assert rows[0].hide_builtin_diagrams is True, "the losing racer's value must still land"


def test_a_read_only_viewer_is_not_offered_a_save_button_that_only_403s(app, client, monkeypatch):
    """Every other write control in Vector respects `vector_can_write`; this page did not, so a
    viewer got a live Save whose only outcome is the host's 403 page. The host gate is still the real
    enforcement — this is the nudge, and it is the one Vector already established."""
    login(app, ALICE)
    assert "<button" in client.get("/vector/settings").get_data(as_text=True)

    monkeypatch.setattr("vector.blueprint.host_can_write", lambda: False)
    body = client.get("/vector/settings").get_data(as_text=True)
    assert "<button" not in body
    assert "read-only" in body
