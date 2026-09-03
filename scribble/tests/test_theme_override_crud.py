"""Tests for `scribble.themes_api` — override report Theme CRUD, upload validation, the admin gate, the
audit trail, and the per-install default (ext#113 + #105).

See `scribble/themes_api.py`'s module docstring for the design this pins:
  * every write is validated through `reporting.theme_files._parse_theme_toml` -- the SAME parser a
    bundled Theme file is checked against -- and an invalid submission stores nothing;
  * a name colliding with a bundled (or installed) Theme is refused, bundled/installed always win;
  * every mutating route requires `_actor_is_admin()`, and records exactly one audit row with
    before/after through `api_pat._audit` (INV-AUDIT-03);
  * deleting or renaming the override Theme the per-install default currently names must not orphan
    that setting.
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select

# `scribble/__init__.py::_wire_feature_routes` is the orchestrator's integration point (out of this
# ticket's file ownership) and does not yet import `themes_api`. Register this module's routes onto the
# process-singleton blueprints directly, at MODULE IMPORT TIME -- i.e. before `tests/conftest.py`'s
# `app` fixture ever calls `scribble.register()` for the first test in this file, which is exactly the
# "before the first app.register_blueprint" ordering `_wire_feature_routes` itself is built around (see
# that function's docstring) -- Flask forbids adding routes to a blueprint that has already been
# registered on an app. `themes_api.register` guards on its own `_REGISTERED` flag, so once the
# orchestrator adds it to `_wire_feature_routes` for real, this becomes a harmless no-op rather than a
# double registration.
from scribble.api import api_bp
from scribble.blueprint import bp
from scribble.themes_api import register as _register_themes_api

_register_themes_api(api_bp, bp)

from scribble.models import ScribbleSettings, ScribbleThemeOverride  # noqa: E402
from scribble.reporting.theme_discovery import (  # noqa: E402
    InstalledThemeDescriptor,
    ThemeDiscovery,
    ThemeLoadError,
)
from tests.conftest import StubUser, _StubRole  # noqa: E402

API = "/scribble/api/themes"
PAGE = "/scribble/themes"

VALID_TOML = """
[identity]
name = "acme"
label = "Acme Brand"

[tokens]
accent = "#123456"
"""

# A second, distinctly-named valid Theme -- used by the rename test so "before" and "after" are two
# real, independently-valid names rather than a single fixture string reused everywhere.
VALID_TOML_RENAMED = """
[identity]
name = "acme2"
label = "Acme Brand Two"

[tokens]
accent = "#654321"
"""

# Fails `reporting.tokens.validate_tokens`'s closed allowlist: `not_a_real_token` is not in
# `ALLOWED_TOKENS`, so the WHOLE file is refused -- see that module's "wholesale gate" docstring.
INVALID_TOML = """
[identity]
name = "badtoken"
label = "Bad"

[tokens]
not_a_real_token = "#123456"
"""

# `light` is a BUNDLED Theme name (scribble/report_themes/light.toml) -- bundled always wins over an
# override with the same name.
COLLIDING_TOML = """
[identity]
name = "light"
label = "Shadow Light"

[tokens]
accent = "#000000"
"""


def _make_admin(stub_host) -> None:
    stub_host.current_user = StubUser(id=1, username="admin", role=_StubRole("admin"))


def _make_viewer(stub_host) -> None:
    stub_host.current_user = StubUser(id=9, username="some-viewer", role=_StubRole("viewer"))


def _upload(client, toml_text: str):
    return client.post(API, json={"source_toml": toml_text})


def _override_rows(session_factory) -> list[ScribbleThemeOverride]:
    with session_factory() as db:
        return list(db.scalars(select(ScribbleThemeOverride)))


# --------------------------------------------------------------------------- upload / validation


def test_a_valid_upload_is_stored_and_listed_with_override_provenance(client, stub_host, session_factory):
    _make_admin(stub_host)
    resp = _upload(client, VALID_TOML)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["theme"]["name"] == "acme"
    assert body["theme"]["label"] == "Acme Brand"
    assert body["theme"]["provenance"] == "override"

    rows = _override_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].name == "acme"
    assert rows[0].source_toml == VALID_TOML

    listed = client.get(API).get_json()
    matches = [t for t in listed["themes"] if t["name"] == "acme"]
    assert len(matches) == 1
    assert matches[0]["provenance"] == "override"


def test_a_multipart_file_upload_is_accepted_too(client, stub_host, session_factory):
    _make_admin(stub_host)
    resp = client.post(
        API,
        data={"file": (io.BytesIO(VALID_TOML.encode("utf-8")), "acme.toml")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.get_json()
    assert _override_rows(session_factory)[0].name == "acme"


def test_an_invalid_upload_is_refused_with_the_parsers_message_and_stores_nothing(
    client, stub_host, session_factory
):
    _make_admin(stub_host)
    resp = _upload(client, INVALID_TOML)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    # The parser's own message, verbatim -- not a generic failure (ticket requirement).
    assert "closed allowlist" in body["error"]
    assert "badtoken" in body["error"]
    assert _override_rows(session_factory) == []


def test_a_name_colliding_with_a_bundled_theme_is_refused_and_stores_nothing(
    client, stub_host, session_factory
):
    _make_admin(stub_host)
    resp = _upload(client, COLLIDING_TOML)
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert "light" in body["error"]
    assert "bundled" in body["error"]
    assert _override_rows(session_factory) == []


def test_uploading_the_same_name_twice_is_refused_as_a_collision_with_itself(
    client, stub_host, session_factory
):
    _make_admin(stub_host)
    assert _upload(client, VALID_TOML).status_code == 201
    dup = _upload(client, VALID_TOML)
    assert dup.status_code == 409
    assert len(_override_rows(session_factory)) == 1


# --------------------------------------------------------------------------- admin gate


@pytest.mark.parametrize(
    "make_request",
    [
        lambda client, theme_id: client.post(API, json={"source_toml": VALID_TOML}),
        lambda client, theme_id: client.post(f"{API}/{theme_id}", json={"source_toml": VALID_TOML_RENAMED}),
        lambda client, theme_id: client.post(f"{API}/{theme_id}/delete", json={}),
        lambda client, theme_id: client.post(f"{API}/default", json={"name": "light"}),
    ],
    ids=["create", "update", "delete", "set_default"],
)
def test_every_mutating_route_refuses_a_non_admin(client, stub_host, session_factory, make_request):
    """Enumerated, not looped over a shared helper with a silent skip: a new mutating route added later
    without this gate must break THIS test, per the ticket's own requirement."""
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()
    theme_id = created["theme"]["id"]

    _make_viewer(stub_host)
    resp = make_request(client, theme_id)
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json()["ok"] is False

    # Nothing changed: the row (and the install default) is exactly as the admin left it.
    rows = _override_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].name == "acme"
    assert rows[0].source_toml == VALID_TOML


def test_standalone_no_host_actor_is_treated_as_admin(client, session_factory):
    """No `stub_host` fixture at all -- the plain `app`/`client` the autouse `_every_app_gets_a_host_
    object_store` fixture wires (objects/create_engagement/pat_actor only, deliberately no
    `current_actor`/`audit` hook -- see conftest.py). Standalone Scribble has no one else to defer to."""
    resp = _upload(client, VALID_TOML)
    assert resp.status_code == 201, resp.get_json()


# --------------------------------------------------------------------------- CRUD: update / delete


def test_update_revalidates_and_can_rename(client, stub_host, session_factory):
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()["theme"]

    resp = client.post(f"{API}/{created['id']}", json={"source_toml": VALID_TOML_RENAMED})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()["theme"]
    assert body["name"] == "acme2"
    assert body["label"] == "Acme Brand Two"

    rows = _override_rows(session_factory)
    assert len(rows) == 1
    assert rows[0].name == "acme2"
    assert rows[0].source_toml == VALID_TOML_RENAMED


def test_update_with_invalid_toml_is_refused_and_leaves_the_row_untouched(client, stub_host, session_factory):
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()["theme"]

    resp = client.post(f"{API}/{created['id']}", json={"source_toml": INVALID_TOML})
    assert resp.status_code == 400
    rows = _override_rows(session_factory)
    assert rows[0].name == "acme"
    assert rows[0].source_toml == VALID_TOML


def test_update_refuses_renaming_onto_a_bundled_name(client, stub_host, session_factory):
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()["theme"]

    resp = client.post(f"{API}/{created['id']}", json={"source_toml": COLLIDING_TOML})
    assert resp.status_code == 409
    assert _override_rows(session_factory)[0].name == "acme"


def test_delete_removes_the_row(client, stub_host, session_factory):
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()["theme"]

    resp = client.post(f"{API}/{created['id']}/delete", json={})
    assert resp.status_code == 200, resp.get_json()
    assert _override_rows(session_factory) == []


def test_delete_of_the_current_default_clears_the_default_rather_than_orphaning_it(
    client, stub_host, session_factory
):
    """Documented delete behaviour (ticket requirement): the install default is a bare NAME, not a
    foreign key, so deleting the Theme it names must not leave it dangling."""
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()["theme"]
    assert client.post(f"{API}/default", json={"name": "acme"}).status_code == 200

    resp = client.post(f"{API}/{created['id']}/delete", json={})
    assert resp.status_code == 200

    with session_factory() as db:
        settings = db.scalar(select(ScribbleSettings).where(ScribbleSettings.slot == "default"))
        assert settings.default_report_theme is None


def test_rename_of_the_current_default_follows_the_rename_rather_than_orphaning_it(
    client, stub_host, session_factory
):
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()["theme"]
    assert client.post(f"{API}/default", json={"name": "acme"}).status_code == 200

    resp = client.post(f"{API}/{created['id']}", json={"source_toml": VALID_TOML_RENAMED})
    assert resp.status_code == 200

    with session_factory() as db:
        settings = db.scalar(select(ScribbleSettings).where(ScribbleSettings.slot == "default"))
        assert settings.default_report_theme == "acme2"


# --------------------------------------------------------------------------- per-install default


def test_default_theme_round_trips(client, stub_host, session_factory):
    _make_admin(stub_host)
    assert client.get(API).get_json()["default_report_theme"] is None

    resp = client.post(f"{API}/default", json={"name": "light"})
    assert resp.status_code == 200
    assert resp.get_json()["default_report_theme"] == "light"

    listed = client.get(API).get_json()
    assert listed["default_report_theme"] == "light"
    light_row = next(t for t in listed["themes"] if t["name"] == "light")
    assert light_row["is_default"] is True

    # Clearing back to "inherit further" (empty name) round-trips to None.
    resp = client.post(f"{API}/default", json={"name": ""})
    assert resp.status_code == 200
    assert resp.get_json()["default_report_theme"] is None


def test_default_theme_refuses_an_unknown_name(client, stub_host):
    _make_admin(stub_host)
    resp = client.post(f"{API}/default", json={"name": "no-such-theme"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


# --------------------------------------------------------------------------- audit trail


def test_create_emits_exactly_one_audit_row_with_before_and_after(client, stub_host):
    _make_admin(stub_host)
    stub_host.audit_calls.clear()
    created = _upload(client, VALID_TOML).get_json()["theme"]

    assert [a for a, _ in stub_host.audit_calls] == ["ext:scribble:create_override_theme"]
    _, kw = stub_host.audit_calls[0]
    assert kw["subject_type"] == "scribble_theme_override"
    assert str(kw["subject_id"]) == str(created["id"])
    assert kw["before"] is None
    assert kw["after"]["name"] == "acme"
    assert kw["after"]["source_toml"] == VALID_TOML


def test_update_emits_exactly_one_audit_row_with_before_and_after(client, stub_host):
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()["theme"]

    stub_host.audit_calls.clear()
    client.post(f"{API}/{created['id']}", json={"source_toml": VALID_TOML_RENAMED})

    assert [a for a, _ in stub_host.audit_calls] == ["ext:scribble:update_override_theme"]
    _, kw = stub_host.audit_calls[0]
    assert kw["subject_type"] == "scribble_theme_override"
    assert kw["before"]["name"] == "acme"
    assert kw["after"]["name"] == "acme2"


def test_delete_emits_exactly_one_audit_row_with_before_and_no_after(client, stub_host):
    _make_admin(stub_host)
    created = _upload(client, VALID_TOML).get_json()["theme"]

    stub_host.audit_calls.clear()
    client.post(f"{API}/{created['id']}/delete", json={})

    assert [a for a, _ in stub_host.audit_calls] == ["ext:scribble:delete_override_theme"]
    _, kw = stub_host.audit_calls[0]
    assert kw["before"]["name"] == "acme"
    assert kw["after"] is None


def test_set_default_emits_exactly_one_audit_row_with_before_and_after(client, stub_host):
    _make_admin(stub_host)
    stub_host.audit_calls.clear()
    resp = client.post(f"{API}/default", json={"name": "light"})
    assert resp.status_code == 200

    assert [a for a, _ in stub_host.audit_calls] == ["ext:scribble:set_default_theme"]
    _, kw = stub_host.audit_calls[0]
    assert kw["subject_type"] == "scribble_theme_settings"
    assert kw["before"] == {"default_report_theme": None}
    assert kw["after"] == {"default_report_theme": "light"}


# --------------------------------------------------------------------------- the UI page


def test_the_ui_page_renders_for_an_admin_and_lists_all_three_provenances(
    client, stub_host, monkeypatch
):
    _make_admin(stub_host)
    _upload(client, VALID_TOML)

    def _fake_discovery(**_kwargs):
        return ThemeDiscovery(
            themes={
                "acme-installed": InstalledThemeDescriptor(
                    name="acme-installed",
                    provenance="installed",
                    load_toml=lambda: (
                        '[identity]\nname = "acme-installed"\nlabel = "Acme (installed)"\n'
                    ),
                    distribution="acme-brand-pkg",
                ),
            },
            errors=(ThemeLoadError(entry_point_name="broken-pkg", distribution="broken-dist", error="boom"),),
        )

    monkeypatch.setattr("scribble.themes_api.discover_installed_themes", _fake_discovery)

    resp = client.get(PAGE)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "light" in html  # bundled
    assert "acme-installed" in html  # installed
    assert "acme" in html  # override
    assert "pill-bundled" in html
    assert "pill-installed" in html
    assert "pill-override" in html
    # The discovery error is surfaced on the admin page, not swallowed (see the theme_discovery module
    # docstring's "Error surfacing" section, which explicitly invites exactly this).
    assert "boom" in html


def test_the_ui_page_hides_mutating_controls_from_a_non_admin(client, stub_host):
    _make_admin(stub_host)
    _upload(client, VALID_TOML)

    _make_viewer(stub_host)
    resp = client.get(PAGE)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Still visible (read is not gated) ...
    assert "acme" in html
    # ... but no upload form / edit / delete controls for a non-admin. (The page's <script> block
    # always references these ids/classes via getElementById/querySelectorAll regardless of who is
    # looking, so the assertions below check for the actual HTML markup -- heading text and a quoted
    # class attribute -- rather than a bare id/class name that would also match the script text.)
    assert "Add an override Theme" not in html
    assert 'class="btn thm-delete"' not in html

# --------------------------------------------------------------------------- gaps found in review
#
# Three branches the original suite left unexercised. Each is code that already behaves correctly --
# verified by reading -- but had no test that would go red if it regressed, which is the same thing as
# untested for a security gate.


def test_a_mounted_host_with_no_logged_in_user_is_refused(client, stub_host):
    """The FAIL-CLOSED branch of `_actor_is_admin`. Every other authz test either has no host at all
    (genuinely standalone -> permitted) or a logged-in viewer/admin. This is the third state: MOUNTED,
    hook present, but nobody authenticated. It must refuse, and without this test a regression that
    collapsed it into the standalone branch would open every mutating route to anonymous callers."""
    stub_host.current_user = None
    resp = _upload(client, VALID_TOML)
    assert resp.status_code == 403, resp.get_json()
    assert resp.get_json()["ok"] is False


def test_a_name_colliding_with_an_INSTALLED_theme_is_refused(
    client, stub_host, session_factory, monkeypatch
):
    """`_collision_reason` refuses a collision with an installed Theme, not just a bundled one -- the
    bundled case was tested, this one was not. It matters more than the bundled case in one respect:
    the set of bundled names is fixed and visible in the tree, while the installed set depends on
    whatever happens to be pip-installed on this instance, so an operator can hit it by surprise."""
    import scribble.themes_api as ta
    from scribble.reporting.theme_discovery import (
        PROVENANCE_INSTALLED,
        InstalledThemeDescriptor,
        ThemeDiscovery,
    )

    descriptor = InstalledThemeDescriptor(
        name="acme",
        provenance=PROVENANCE_INSTALLED,
        load_toml=lambda: VALID_TOML,
        distribution="lotek-theme-acme",
    )
    # Patch the SEAM in themes_api, not the source module: `themes_api` does
    # `from ...theme_discovery import discover_installed_themes`, so that name is bound at import and
    # patching `theme_discovery.discover_installed_themes` never reaches it. `_installed_descriptors`
    # exists precisely to be this seam.
    monkeypatch.setattr(
        ta, "_installed_descriptors", lambda: ThemeDiscovery(themes={"acme": descriptor})
    )

    _make_admin(stub_host)
    resp = _upload(client, VALID_TOML)  # VALID_TOML declares name = "acme"
    assert resp.status_code == 409, resp.get_json()
    assert "acme" in resp.get_json()["error"]
    assert _override_rows(session_factory) == []


def test_a_unique_violation_at_commit_is_a_409_not_a_500(client, stub_host, monkeypatch):
    """The TOCTOU window: `_collision_reason` runs BEFORE the commit, so two concurrent admins naming
    the same new Theme both pass it and the loser hits the UNIQUE constraint. That is a conflict, and
    reporting it as a 500 would send an operator hunting a server fault for their own duplicate name.
    Simulated by making the commit raise, since a real race is not reproducible in a test."""
    from sqlalchemy.exc import IntegrityError

    _make_admin(stub_host)
    import scribble.themes_api as ta

    real_settings = ta._get_or_create_settings

    def boom(db):
        settings = real_settings(db)
        original_commit = db.commit

        def failing_commit():
            db.commit = original_commit  # let the rollback path work normally afterwards
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

        db.commit = failing_commit
        return settings

    monkeypatch.setattr(ta, "_get_or_create_settings", boom)
    resp = _upload(client, VALID_TOML)
    assert resp.status_code == 409, resp.get_json()
    assert resp.get_json()["ok"] is False
