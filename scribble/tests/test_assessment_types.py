"""WS13 tests: AssessmentType admin UI + API (scribble/assessment_types_ui.py).

Self-contained: wires ``assessment_types_ui.register(api_bp, bp)`` onto the *shared* blueprint
singletons before ``scribble.register()`` attaches them to any app -- exactly what the driver will
eventually do inside ``scribble/__init__.py::_wire_feature_routes`` (see tests/test_artifacts.py for the
same pattern applied to WS5). ``register`` is idempotent, so repeating this is always safe.

Unlike the already-integrated workstreams (whose hooks run inside ``_wire_feature_routes`` on the very
first ``scribble.register()`` call of the whole process, before any blueprint is attached to any app),
this module is NOT wired in yet -- the driver adds that. So this call has to win a race: Flask forbids
adding routes to a blueprint once it has been registered on *any* app (raises ``AssertionError``), and
some other test module's ``app`` fixture calls plain ``scribble.register()`` at *test setup* time. Since
pytest fully *collects* (imports) every test module before *running* any test in the session, doing the
registration at module import time (here, not inside the ``app`` fixture) guarantees it happens before
the first test's setup anywhere in the suite -- regardless of file collection order.

Per docs/RAILS.md #4, every assertion below checks the persisted row / real end-state (row counts, the
same primary key after an edit, the actual `active` flag, whether a `FindingGroup.assessment_type_id`
survived a refused delete) rather than just an HTTP status code.
"""

from __future__ import annotations

import uuid

import pytest
from flask import Flask
from sqlalchemy import create_engine, event, select

import scribble
from scribble import assessment_types_ui
from scribble.api import api_bp
from scribble.blueprint import bp
from scribble.models import AssessmentType, Client, Engagement, FindingGroup
from scribble.seed import seed_defaults

API_PREFIX = "/scribble/api"

# Must run at import (collection) time -- see module docstring for why this can't live inside the
# `app` fixture the way tests/test_artifacts.py's does.
assessment_types_ui.register(api_bp, bp)


@pytest.fixture
def app(tmp_path):
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp_path), base_template="scribble/base.html"
    )
    with cfg.session_factory() as session:
        seed_defaults(session)
        session.commit()
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(app):
    return app.extensions["scribble"].session_factory


def _make_type(db, **overrides) -> AssessmentType:
    defaults = dict(name="zzCloud", slug="zzcloud", color="#123456", default_order=9, active=True)
    defaults.update(overrides)
    t = AssessmentType(**defaults)
    db.add(t)
    db.commit()
    return t


def _make_engagement_with_group(db, assessment_type: AssessmentType) -> FindingGroup:
    client_row = Client(name="zzAcme")
    db.add(client_row)
    db.flush()
    engagement = Engagement(name="zzQ3", client_id=client_row.id, company_name="zzAcme Corp")
    group = FindingGroup(
        engagement=engagement, assessment_type=assessment_type, name="zzSection", order_index=0
    )
    db.add_all([engagement, group])
    db.commit()
    return group


def _make_fk_app(tmp_path):
    """Build a Flask app whose SQLite engine enforces foreign keys (``PRAGMA foreign_keys=ON`` per
    connection). Standalone SQLite has FK enforcement OFF by default; the driver enables it at the DB
    layer, so this mirrors an FK-enforcing host to prove the delete handler degrades cleanly (400, not
    a 500, and no orphaned ``FindingGroup.assessment_type_id``) rather than relying on the soft default.
    Returns ``(app, session_factory)``.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'fk.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record):  # pragma: no cover - trivial pragma wiring
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test"
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp_path), base_template="scribble/base.html"
    )
    with cfg.session_factory() as session:
        seed_defaults(session)
        session.commit()
    return flask_app, cfg.session_factory


# --------------------------------------------------------------------------------------- register()


def test_register_is_idempotent():
    assessment_types_ui.register(api_bp, bp)
    assessment_types_ui.register(api_bp, bp)


# --------------------------------------------------------------------------------------- list page


def test_assessment_types_page_renders(client):
    resp = client.get("/scribble/assessment-types")
    assert resp.status_code == 200


def test_page_lists_seeded_defaults(client):
    # scribble/seed/loader.py ships four defaults; the admin page must not hide them.
    resp = client.get("/scribble/assessment-types")
    for name in (b"Internal", b"External", b"Web App", b"Device / Mobile"):
        assert name in resp.data


def test_page_lists_both_active_and_inactive_rows(client, session_factory):
    with session_factory() as db:
        _make_type(db, name="zzActiveType", slug="zzactive-type", active=True)
        _make_type(db, name="zzInactiveType", slug="zzinactive-type", active=False)

    resp = client.get("/scribble/assessment-types")
    assert b"zzActiveType" in resp.data
    assert b"zzInactiveType" in resp.data


def test_page_shows_finding_group_reference_count(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzReferenced", slug="zzreferenced")
        _make_engagement_with_group(db, t)

    resp = client.get("/scribble/assessment-types")
    assert b"Referenced by 1 finding group(s)" in resp.data


# --------------------------------------------------------------------------------------- create


def test_create_persists_row_with_given_fields(client, session_factory):
    resp = client.post(
        f"{API_PREFIX}/assessment-types",
        json={"name": "zzCreated Type", "slug": "zz-created-type", "color": "#abcdef", "default_order": 5},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["ok"] is True
    new_id = data["id"]

    with session_factory() as db:
        t = db.get(AssessmentType, new_id)
        assert t is not None
        assert t.name == "zzCreated Type"
        assert t.slug == "zz-created-type"
        assert t.color == "#abcdef"
        assert t.default_order == 5
        assert t.active is True


def test_create_auto_derives_slug_from_name_when_omitted(client, session_factory):
    resp = client.post(f"{API_PREFIX}/assessment-types", json={"name": "zzCloud Native!!"})
    assert resp.status_code == 201
    new_id = uuid.UUID(resp.get_json()["id"])

    with session_factory() as db:
        t = db.get(AssessmentType, new_id)
        assert t.slug == "zzcloud-native"


def test_create_requires_name(client, session_factory):
    with session_factory() as db:
        count_before = db.query(AssessmentType).count()

    resp = client.post(f"{API_PREFIX}/assessment-types", json={"name": "   "})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    with session_factory() as db:
        assert db.query(AssessmentType).count() == count_before


def test_create_duplicate_name_rejected_and_no_row_added(client, session_factory):
    with session_factory() as db:
        _make_type(db, name="zzDupName", slug="zzdup-name-orig")
        count_before = db.query(AssessmentType).count()

    resp = client.post(
        f"{API_PREFIX}/assessment-types", json={"name": "zzDupName", "slug": "zzdup-name-different"}
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    with session_factory() as db:
        assert db.query(AssessmentType).count() == count_before


def test_create_duplicate_name_case_insensitive_rejected(client, session_factory):
    with session_factory() as db:
        _make_type(db, name="zzCaseName", slug="zzcase-name")
        count_before = db.query(AssessmentType).count()

    resp = client.post(
        f"{API_PREFIX}/assessment-types",
        json={"name": "zzcasename".upper(), "slug": "zzcase-name-2"},
    )
    assert resp.status_code == 400

    with session_factory() as db:
        assert db.query(AssessmentType).count() == count_before


def test_create_duplicate_slug_rejected_and_no_row_added(client, session_factory):
    with session_factory() as db:
        _make_type(db, name="zzOriginalForSlug", slug="zzshared-slug")
        count_before = db.query(AssessmentType).count()

    resp = client.post(
        f"{API_PREFIX}/assessment-types",
        json={"name": "zzDifferentName", "slug": "zzshared-slug"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    with session_factory() as db:
        assert db.query(AssessmentType).count() == count_before
        # The original row is untouched -- still the sole owner of the slug.
        original = db.scalar(select(AssessmentType).where(AssessmentType.slug == "zzshared-slug"))
        assert original.name == "zzOriginalForSlug"


# --------------------------------------------------------------------------------------- edit-in-place


def test_update_renames_same_row_in_place(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzOriginalName", slug="zzoriginal-name")
        type_id = t.id
        count_before = db.query(AssessmentType).count()

    resp = client.post(f"{API_PREFIX}/assessment-types/{type_id}", json={"name": "zzRenamed"})
    assert resp.status_code == 200
    assert resp.get_json()["id"] == type_id

    with session_factory() as db:
        assert db.query(AssessmentType).count() == count_before
        reloaded = db.get(AssessmentType, type_id)
        assert reloaded.id == type_id
        assert reloaded.name == "zzRenamed"


def test_update_recolors_and_reorders_same_row(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzRecolorMe", slug="zzrecolor-me", color="#000000", default_order=0)
        type_id = t.id

    resp = client.post(
        f"{API_PREFIX}/assessment-types/{type_id}",
        json={"color": "#ff00ff", "default_order": 42},
    )
    assert resp.status_code == 200

    with session_factory() as db:
        reloaded = db.get(AssessmentType, type_id)
        assert reloaded.id == type_id
        assert reloaded.color == "#ff00ff"
        assert reloaded.default_order == 42
        # untouched fields survive a partial update
        assert reloaded.name == "zzRecolorMe"
        assert reloaded.slug == "zzrecolor-me"


def test_update_rejects_rename_to_existing_name(client, session_factory):
    with session_factory() as db:
        _make_type(db, name="zzTaken", slug="zztaken")
        victim = _make_type(db, name="zzVictim", slug="zzvictim")
        victim_id = victim.id

    resp = client.post(f"{API_PREFIX}/assessment-types/{victim_id}", json={"name": "zzTaken"})
    assert resp.status_code == 400

    with session_factory() as db:
        reloaded = db.get(AssessmentType, victim_id)
        assert reloaded.name == "zzVictim"  # unchanged


def test_update_missing_id_404(client):
    resp = client.post(f"{API_PREFIX}/assessment-types/999999", json={"name": "zzNope"})
    assert resp.status_code == 404


def test_deactivate_hides_from_active_query_but_keeps_row(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzToDeactivate", slug="zzto-deactivate", active=True)
        type_id = t.id

    resp = client.post(f"{API_PREFIX}/assessment-types/{type_id}", json={"active": False})
    assert resp.status_code == 200

    with session_factory() as db:
        reloaded = db.get(AssessmentType, type_id)
        assert reloaded is not None  # row kept
        assert reloaded.active is False

        active_ids = {
            row_id
            for (row_id,) in db.execute(
                select(AssessmentType.id).where(AssessmentType.active.is_(True))
            ).all()
        }
        assert type_id not in active_ids

    # The admin list itself still shows it (it lists active + inactive) -- inactive is surfaced, not erased.
    page = client.get("/scribble/assessment-types")
    assert b"zzToDeactivate" in page.data


def test_reactivate_via_update(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzReactivateMe", slug="zzreactivate-me", active=False)
        type_id = t.id

    resp = client.post(f"{API_PREFIX}/assessment-types/{type_id}", json={"active": True})
    assert resp.status_code == 200

    with session_factory() as db:
        assert db.get(AssessmentType, type_id).active is True


# --------------------------------------------------------------------------------------- delete


def test_delete_refused_when_referenced_by_finding_group(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzInUse", slug="zzin-use")
        type_id = t.id
        group = _make_engagement_with_group(db, t)
        group_id = group.id
        count_before = db.query(AssessmentType).count()

    resp = client.post(f"{API_PREFIX}/assessment-types/{type_id}/delete")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    with session_factory() as db:
        # Row still present -- never orphaned or hard-deleted out from under the group.
        assert db.query(AssessmentType).count() == count_before
        reloaded = db.get(AssessmentType, type_id)
        assert reloaded is not None
        # The group's FK is completely untouched.
        reloaded_group = db.get(FindingGroup, group_id)
        assert reloaded_group.assessment_type_id == type_id


def test_delete_removes_unreferenced_type(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzUnused", slug="zzunused")
        type_id = t.id
        count_before = db.query(AssessmentType).count()

    resp = client.post(f"{API_PREFIX}/assessment-types/{type_id}/delete")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with session_factory() as db:
        assert db.query(AssessmentType).count() == count_before - 1
        assert db.get(AssessmentType, type_id) is None


def test_delete_missing_id_404(client):
    resp = client.post(f"{API_PREFIX}/assessment-types/999999/delete")
    assert resp.status_code == 404


# ------------------------------------------------------ N1: update-path uniqueness (slug + case)


def test_update_rejects_slug_collision_with_another_row(client, session_factory):
    # Renaming ONE row's slug into ANOTHER row's slug must be refused; neither row changes.
    with session_factory() as db:
        keeper = _make_type(db, name="zzSlugKeeper", slug="zzslug-keeper")
        victim = _make_type(db, name="zzSlugVictim", slug="zzslug-victim")
        keeper_id, victim_id = keeper.id, victim.id

    resp = client.post(
        f"{API_PREFIX}/assessment-types/{victim_id}", json={"slug": "zzslug-keeper"}
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    with session_factory() as db:
        assert db.get(AssessmentType, victim_id).slug == "zzslug-victim"  # unchanged
        assert db.get(AssessmentType, keeper_id).slug == "zzslug-keeper"  # unchanged


def test_update_rejects_case_insensitive_name_collision(client, session_factory):
    # "ZZTAKEN" must collide with an existing "zzTakenName" on rename; victim unchanged.
    with session_factory() as db:
        _make_type(db, name="zzTakenName", slug="zztaken-name")
        victim = _make_type(db, name="zzVictimName", slug="zzvictim-name")
        victim_id = victim.id

    resp = client.post(
        f"{API_PREFIX}/assessment-types/{victim_id}", json={"name": "zzTakenName".upper()}
    )
    assert resp.status_code == 400

    with session_factory() as db:
        assert db.get(AssessmentType, victim_id).name == "zzVictimName"  # unchanged


# ------------------------------------------------------ N2: id is not mass-assignable / repointable


def test_create_ignores_client_supplied_id(client, session_factory):
    # A caller-supplied "id" must NOT be honored -- the DB assigns the primary key.
    resp = client.post(
        f"{API_PREFIX}/assessment-types",
        json={"id": 999999, "name": "zzMassAssignCreate", "slug": "zzmass-assign-create"},
    )
    assert resp.status_code == 201
    new_id = uuid.UUID(resp.get_json()["id"])
    assert new_id != 999999

    with session_factory() as db:
        # No row landed at the attacker-chosen id.
        assert db.get(AssessmentType, 999999) is None
        created = db.scalar(
            select(AssessmentType).where(AssessmentType.slug == "zzmass-assign-create")
        )
        assert created.id == new_id


def test_update_ignores_client_supplied_id(client, session_factory):
    # Posting an "id" in the update body must NOT repoint the row or create a new one.
    with session_factory() as db:
        t = _make_type(db, name="zzMassAssignUpdate", slug="zzmass-assign-update")
        type_id = t.id
        count_before = db.query(AssessmentType).count()

    resp = client.post(
        f"{API_PREFIX}/assessment-types/{type_id}",
        json={"id": 999999, "name": "zzMassAssignRenamed"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["id"] == type_id  # same row

    with session_factory() as db:
        assert db.query(AssessmentType).count() == count_before  # no phantom row
        assert db.get(AssessmentType, 999999) is None
        reloaded = db.get(AssessmentType, type_id)
        assert reloaded.id == type_id
        assert reloaded.name == "zzMassAssignRenamed"


# ------------------------------------------------------ N4: inactive row is surfaced AS inactive


def test_inactive_row_rendered_with_inactive_marker(client, session_factory):
    # Not merely "the name appears" -- the row must carry the inactive class + pill so a user can
    # tell it apart from an active one (RAILS #4: assert the real end-state the user sees).
    with session_factory() as db:
        t = _make_type(db, name="zzInactiveMarker", slug="zzinactive-marker", active=False)
        type_id = t.id

    resp = client.get("/scribble/assessment-types")
    body = resp.data.decode()
    assert "zzInactiveMarker" in body

    # Find the <tr ...> for this row and assert it is marked inactive.
    marker = f'data-at-id="{type_id}"'
    idx = body.index(marker)
    row_start = body.rfind("<tr", 0, idx)
    row_html = body[row_start : idx + 400]
    assert "fr-at-row-inactive" in row_html
    assert "inactive" in row_html  # the "inactive" pill text


def test_active_row_not_marked_inactive(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzActiveMarker", slug="zzactive-marker", active=True)
        type_id = t.id

    body = client.get("/scribble/assessment-types").data.decode()
    marker = f'data-at-id="{type_id}"'
    idx = body.index(marker)
    row_start = body.rfind("<tr", 0, idx)
    row_html = body[row_start : idx + 400]
    assert "fr-at-row-inactive" not in row_html


# ------------------------------------------------------ W1: delete hardening under FK enforcement


def test_delete_referenced_type_under_fk_enforcement_degrades_cleanly(tmp_path):
    # With PRAGMA foreign_keys=ON (mirroring the FK-enforcing host DB the driver is enabling), a
    # delete of a referenced type must return 400 (not 500) and leave the group's FK intact.
    app, sf = _make_fk_app(tmp_path)
    fk_client = app.test_client()
    with sf() as db:
        t = _make_type(db, name="zzFkInUse", slug="zzfk-in-use")
        type_id = t.id
        group = _make_engagement_with_group(db, t)
        group_id = group.id

    resp = fk_client.post(f"{API_PREFIX}/assessment-types/{type_id}/delete")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    with sf() as db:
        assert db.get(AssessmentType, type_id) is not None  # row survives
        assert db.get(FindingGroup, group_id).assessment_type_id == type_id  # FK intact


def test_delete_integrityerror_branch_catches_toctou_race(tmp_path, monkeypatch):
    """Exercise the IntegrityError guard SPECIFICALLY (not the pre-check).

    Forces the TOCTOU window the pre-check can't close by monkeypatching ``_reference_count`` to
    report 0 while the DB (with FK enforcement ON) actually has a referencing group. The pre-check
    passes, the commit's DELETE is rejected by the FK, and the ``except IntegrityError`` branch must
    turn that into a clean 400 with the group's FK intact. Without the try/except this raises (500)
    on an FK-enforcing DB, or silently orphans the FK on a non-enforcing one -- so this test fails
    without the W1 guard and passes with it.
    """
    app, sf = _make_fk_app(tmp_path)
    fk_client = app.test_client()
    with sf() as db:
        t = _make_type(db, name="zzToctou", slug="zztoctou")
        type_id = t.id
        group = _make_engagement_with_group(db, t)
        group_id = group.id

    monkeypatch.setattr(assessment_types_ui, "_reference_count", lambda db, tid: 0)

    resp = fk_client.post(f"{API_PREFIX}/assessment-types/{type_id}/delete")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    with sf() as db:
        assert db.get(AssessmentType, type_id) is not None  # not deleted
        assert db.get(FindingGroup, group_id).assessment_type_id == type_id  # not orphaned


# ------------------------------------------------------ W2: color must be a hex value (CSS-injection)


def test_create_rejects_non_hex_color_and_adds_no_row(client, session_factory):
    with session_factory() as db:
        count_before = db.query(AssessmentType).count()

    resp = client.post(
        f"{API_PREFIX}/assessment-types",
        json={"name": "zzBadColor", "color": "red;background-image:url(https://evil/x)"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    with session_factory() as db:
        assert db.query(AssessmentType).count() == count_before  # nothing persisted


def test_create_accepts_hex_color(client, session_factory):
    resp = client.post(
        f"{API_PREFIX}/assessment-types",
        json={"name": "zzGoodColor", "slug": "zzgood-color", "color": "#Ab12Ef"},
    )
    assert resp.status_code == 201
    with session_factory() as db:
        t = db.get(AssessmentType, uuid.UUID(resp.get_json()["id"]))
        assert t.color == "#Ab12Ef"


def test_create_allows_blank_color_as_none(client, session_factory):
    resp = client.post(
        f"{API_PREFIX}/assessment-types", json={"name": "zzNoColor", "slug": "zzno-color", "color": ""}
    )
    assert resp.status_code == 201
    with session_factory() as db:
        assert db.get(AssessmentType, uuid.UUID(resp.get_json()["id"])).color is None


def test_update_rejects_non_hex_color_and_leaves_row_unchanged(client, session_factory):
    with session_factory() as db:
        t = _make_type(db, name="zzColorGuard", slug="zzcolor-guard", color="#123456")
        type_id = t.id

    resp = client.post(
        f"{API_PREFIX}/assessment-types/{type_id}",
        json={"color": "url(javascript:alert(1))"},
    )
    assert resp.status_code == 400

    with session_factory() as db:
        assert db.get(AssessmentType, type_id).color == "#123456"  # original color preserved
