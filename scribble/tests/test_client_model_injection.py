"""Integration tests for consuming a host-injected ``client_model``/``severity_enum`` end to end
(PLAN.md §19, docs/LOTEK_ADOPTION.md §3.1/§3.2) -- the actual write/read sites, not just the resolvers
unit-tested in ``tests/test_deps.py``.

``Engagement.client_id`` is a soft reference (no FK, no static ``.client`` relationship -- see
``scribble/models.py::Engagement``): standalone it points at ``scribble_clients``; mounted, it should
point at the HOST's own client table instead, with nothing ever written to ``scribble_clients``. These
tests build a tiny stand-in "host" client model + table on the SAME engine (mirroring how Lotek's real
``Client`` would be injected) to prove the repoint is real, not a shadow-table sync.
"""

from __future__ import annotations

import enum
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import Integer, String, Uuid, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import scribble
from scribble.enums import Severity
from scribble.models import Client, Engagement, EngagementFinding
from scribble.seed import seed_defaults

UI = "/scribble"


class _HostBase(DeclarativeBase):
    """A separate declarative registry, exactly like Lotek's own ``Base`` would be -- proves the
    resolver doesn't require the host model to share Scribble's ``scribble.db.Base``."""


class HostClient(_HostBase):
    """Stand-in for a host's own ``Client`` model (e.g. Lotek's), mapped to a table on the SAME shared
    engine Scribble is mounted against."""

    __tablename__ = "host_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)


class HostClientUuid(_HostBase):
    """Stand-in for a Lotek v2 host's ``Client`` model: a UUIDv7 surrogate PK, not a sequential int (see
    plans/v2-rearchitecture-decision.md). Same shared-engine setup as ``HostClient`` above -- this is the
    exact shape that made ``Engagement.client_id``'s old ``Integer`` column and ``engagement_ui._as_int``
    silently drop a v2 host's client link (they assumed every host client id is an int)."""

    __tablename__ = "host_clients_uuid"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True)


@pytest.fixture
def app(tmp_path):
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)

    # The host's own tables live on the SAME engine Scribble mounts against (mirrors Lotek: one shared
    # SQLite DB, Scribble's scribble_* tables additive alongside Lotek's own).
    _HostBase.metadata.create_all(engine)

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


# ------------------------------------------------------------------------- client_model: standalone


def test_engagement_create_resolves_scribbles_own_client_standalone(client, session_factory):
    """No client_model injected (the default): engagement_new's select-or-create writes to
    scribble_clients, and Engagement.resolve_client reads it back -- the pre-existing behavior,
    unchanged by the refactor (also covered end-to-end in tests/test_board.py)."""
    resp = client.post(
        f"{UI}/engagements/new",
        data={"name": "Standalone Co Pentest", "new_client_name": "Standalone Co"},
    )
    assert resp.status_code == 302

    with session_factory() as db:
        eng = db.query(Engagement).filter_by(name="Standalone Co Pentest").one()
        resolved = eng.resolve_client(db)
        assert isinstance(resolved, Client)
        assert resolved.name == "Standalone Co"
        assert db.scalar(select(func.count()).select_from(Client)) == 1


# --------------------------------------------------------------------------- client_model: mounted


def test_engagement_create_repoints_to_injected_host_client_model(client, session_factory, app):
    """With client_model injected: engagement_new's select-or-create writes to the HOST's table (not
    scribble_clients), and Engagement.resolve_client reads a HostClient back -- proving a real repoint,
    not a scribble_clients shadow sync (docs/LOTEK_ADOPTION.md §3.1, "option a", the clean end-state)."""
    cfg = app.extensions["scribble"]
    cfg.client_model = HostClient
    try:
        resp = client.post(
            f"{UI}/engagements/new",
            data={"name": "Hosted Co Pentest", "new_client_name": "Hosted Co"},
        )
        assert resp.status_code == 302

        # resolve_client() calls client_model(), which reads the mounted config off `current_app` --
        # exactly like the real read sites (engagement_board, build_report_context) do from inside a
        # request. Push an app context here to mirror that (client_model()'s RuntimeError guard is a
        # standalone-safety fallback, not something a real read site hits).
        with app.app_context(), session_factory() as db:
            eng = db.query(Engagement).filter_by(name="Hosted Co Pentest").one()
            resolved = eng.resolve_client(db)
            assert isinstance(resolved, HostClient)
            assert resolved.name == "Hosted Co"

            # The real proof this is a REPOINT, not a shadow table: nothing landed in scribble_clients,
            # and exactly one row landed in the host's own table.
            assert db.scalar(select(func.count()).select_from(Client)) == 0
            assert db.scalar(select(func.count()).select_from(HostClient)) == 1
    finally:
        cfg.client_model = None


def test_engagement_create_reuses_existing_injected_host_client_by_id(client, session_factory, app):
    """``client_id`` (an existing host client, selected from the dropdown rather than typed as a new
    name) round-trips through the same select-or-create path when a host model is injected."""
    cfg = app.extensions["scribble"]
    cfg.client_model = HostClient
    try:
        with session_factory() as db:
            existing = HostClient(name="Existing Hosted Co")
            db.add(existing)
            db.commit()
            existing_id = existing.id

        resp = client.post(
            f"{UI}/engagements/new",
            data={"name": "Reuse Hosted Co Pentest", "client_id": str(existing_id)},
        )
        assert resp.status_code == 302

        with app.app_context(), session_factory() as db:
            eng = db.query(Engagement).filter_by(name="Reuse Hosted Co Pentest").one()
            assert eng.client_id == existing_id
            resolved = eng.resolve_client(db)
            assert isinstance(resolved, HostClient)
            assert resolved.name == "Existing Hosted Co"
            assert db.scalar(select(func.count()).select_from(HostClient)) == 1
    finally:
        cfg.client_model = None


def test_engagement_create_links_uuid_client_id_when_mounted_host_uses_uuid_ids(client, session_factory, app):
    """Lotek v2 host client ids are UUIDs, not ints (see plans/v2-rearchitecture-decision.md). The old
    ``engagement_ui._as_int(form.get("client_id"))`` parsed a UUID string to ``None`` -- a valid,
    intentionally-selected client link silently dropped on create. This proves the create form's
    ``client_id`` field now round-trips a UUID AND that the link is real -- ``resolve_client`` actually
    resolves the HostClientUuid row, not just an id sitting unresolved in the column."""
    cfg = app.extensions["scribble"]
    cfg.client_model = HostClientUuid
    try:
        with session_factory() as db:
            existing = HostClientUuid(name="Hosted Co (v2)")
            db.add(existing)
            db.commit()
            existing_id = existing.id
        assert isinstance(existing_id, uuid.UUID)

        resp = client.post(
            f"{UI}/engagements/new",
            data={"name": "Hosted Co v2 Pentest", "client_id": str(existing_id)},
        )
        assert resp.status_code == 302

        with app.app_context(), session_factory() as db:
            eng = db.query(Engagement).filter_by(name="Hosted Co v2 Pentest").one()
            assert eng.client_id == existing_id, "the UUID client link must NOT be dropped"
            assert isinstance(eng.client_id, uuid.UUID)
            resolved = eng.resolve_client(db)
            assert isinstance(resolved, HostClientUuid)
            assert resolved.name == "Hosted Co (v2)"
    finally:
        cfg.client_model = None


def test_engagement_create_persists_uuid_owner_id_when_mounted_host_uses_uuid_ids(
    client, session_factory, app
):
    """The other half of the same bug: ``scribble.deps.current_actor_id()`` fed ``Engagement.owner_id``
    attribution. Its old ``isinstance(ident, int)`` check silently turned a v2 host's UUID actor id into
    ``None`` -- ``owner_id`` NULL on every mounted create, no error, attribution just gone. Proves it now
    persists as the real UUID, not None."""
    cfg = app.extensions["scribble"]
    actor_id = uuid.uuid4()
    cfg.extras["current_actor"] = lambda: SimpleNamespace(id=actor_id, username="v2.operator")
    try:
        resp = client.post(f"{UI}/engagements/new", data={"name": "Attributed v2 Pentest"})
        assert resp.status_code == 302

        with session_factory() as db:
            eng = db.query(Engagement).filter_by(name="Attributed v2 Pentest").one()
            assert eng.owner_id is not None, "owner_id must not be silently dropped for a UUID actor id"
            assert eng.owner_id == actor_id
            assert isinstance(eng.owner_id, uuid.UUID)
    finally:
        cfg.extras.pop("current_actor", None)


def test_dashboard_and_health_client_counts_reflect_injected_host_model(client, session_factory, app):
    """blueprint.py's dashboard tile and api.py's /health count -- both switched from a hardcoded
    scribble.models.Client to client_model() so they don't silently read an always-empty
    scribble_clients table once client creation has moved to the host's table."""
    cfg = app.extensions["scribble"]
    cfg.client_model = HostClient
    try:
        with session_factory() as db:
            db.add(HostClient(name="Counted Co"))
            db.commit()

        resp = client.get(f"{UI}/api/health")
        assert resp.get_json()["counts"]["clients"] == 1

        resp = client.get(f"{UI}/")
        assert resp.status_code == 200
    finally:
        cfg.client_model = None


# ------------------------------------------------------------------------- severity_enum: boundary


def _host_severity_enum():
    class HostSeverity(enum.StrEnum):
        info = "info"
        low = "low"
        medium = "medium"
        high = "high"
        critical = "critical"

    return HostSeverity


def test_from_lotek_finding_uses_scribbles_own_severity_standalone(app):
    fake_finding = SimpleNamespace(title="SQLi", severity=SimpleNamespace(value="high"))
    with app.app_context():
        finding = EngagementFinding.from_lotek_finding(fake_finding)
    assert finding.severity == Severity.high
    assert isinstance(finding.severity, Severity)


def test_from_lotek_finding_uses_injected_host_severity_when_mounted(app):
    HostSeverity = _host_severity_enum()
    cfg = app.extensions["scribble"]
    cfg.severity_enum = HostSeverity
    try:
        fake_finding = SimpleNamespace(title="SQLi", severity=SimpleNamespace(value="high"))
        with app.app_context():
            finding = EngagementFinding.from_lotek_finding(fake_finding)
        # Value-identical (docs/LOTEK_ADOPTION.md §3.2) so this still equals scribble's own Severity.high
        # by value/hash -- the real assertion is the OBJECT IDENTITY of the constructed enum member.
        assert finding.severity == Severity.high
        assert isinstance(finding.severity, HostSeverity)
        assert not isinstance(finding.severity, Severity)
    finally:
        cfg.severity_enum = None


def test_from_lotek_finding_defaults_safely_with_no_app_context_at_all():
    """No Flask app pushed at all (e.g. a script driving scribble.models directly): severity_enum()'s
    RuntimeError guard falls back to scribble.enums.Severity rather than raising."""
    fake_finding = SimpleNamespace(title="SQLi", severity=SimpleNamespace(value="critical"))
    finding = EngagementFinding.from_lotek_finding(fake_finding)
    assert finding.severity == Severity.critical
    assert isinstance(finding.severity, Severity)


def test_from_lotek_finding_sets_source_finding_id_from_the_lotek_finding():
    """Promoting a Lotek scan finding stamps ``source_finding_id`` from the source's own id, so the
    promote flow can later dedup (has this Lotek finding already been promoted here?)."""
    fake_finding = SimpleNamespace(id=42, title="SQLi", severity=SimpleNamespace(value="high"))
    finding = EngagementFinding.from_lotek_finding(fake_finding)
    assert finding.source_finding_id == 42


def test_from_lotek_finding_source_finding_id_override_wins():
    """An explicit ``source_finding_id=`` override (e.g. re-pointing at a different id) beats the
    finding's own id, matching the general override-merge pattern of ``from_lotek_finding``."""
    fake_finding = SimpleNamespace(id=42, title="SQLi", severity=SimpleNamespace(value="high"))
    finding = EngagementFinding.from_lotek_finding(fake_finding, source_finding_id=99)
    assert finding.source_finding_id == 99
