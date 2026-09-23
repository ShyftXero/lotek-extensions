"""Integration tests for consuming a host-injected ``client_model``/``severity_enum`` end to end
(PLAN.md §19, docs/LOTEK_ADOPTION.md §3.1/§3.2) -- the actual write/read sites, not just the resolvers
unit-tested in ``tests/test_deps.py``.

``ReportBoard.client_id`` is a soft reference (no FK, no static ``.client`` relationship -- see
``scribble/models.py::ReportBoard``): standalone it points at ``scribble_clients``; mounted, it should
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
from scribble.models import BoardFinding, Client, ReportBoard
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
    exact shape that made ``ReportBoard.client_id``'s old ``Integer`` column and ``engagement_ui._as_int``
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


# --------------------------------------------------------------------------- client_model: mounted
#
# Scribble no longer CREATES clients: the one board-create path (Import-to-Scribble) links a board to a
# core engagement and DERIVES its client_id from the host's own scoped summary. So the old "select-or-
# create writes to the host table, not a scribble_clients shadow" tests are gone with the create form
# they exercised — there is nothing to shadow-sync when scribble writes no client at all. What still
# matters, and is proven below, is that a host client id (int or UUID) linked via import round-trips
# through SoftHostId and resolves the real host-table row, and that owner_id (a UUID actor id) persists.


def _import(client, cfg, *, core_id, name, client_id):
    """Drive the one create path (Import-to-Scribble) on this unmounted test app: inject the two host
    hooks import_board reads (the operator gate + the scoped summary it derives name/client from), then
    POST the core id. Caller pops the two hooks in a finally."""
    cfg.extras["can_operate_on"] = lambda _id: True
    cfg.extras["engagement_summaries"] = lambda: [
        {"id": core_id, "name": name, "client_id": client_id, "client_name": name}
    ]
    return client.post(f"{UI}/engagements/import", data={"core_engagement_id": str(core_id)})


def test_import_links_an_existing_host_client_by_id(client, session_factory, app):
    """The core summary carries an existing host client id; import stores it and resolve_client reads the
    HostClient back, with nothing written to scribble_clients."""
    cfg = app.extensions["scribble"]
    cfg.client_model = HostClient
    core = uuid.uuid7()
    try:
        with session_factory() as db:
            existing = HostClient(name="Existing Hosted Co")
            db.add(existing)
            db.commit()
            existing_id = existing.id

        resp = _import(client, cfg, core_id=core, name="Reuse Hosted Co Pentest", client_id=existing_id)
        assert resp.status_code == 302

        with app.app_context(), session_factory() as db:
            eng = db.query(ReportBoard).filter_by(name="Reuse Hosted Co Pentest").one()
            assert eng.client_id == existing_id
            resolved = eng.resolve_client(db)
            assert isinstance(resolved, HostClient)
            assert resolved.name == "Existing Hosted Co"
            assert db.scalar(select(func.count()).select_from(HostClient)) == 1
            assert db.scalar(select(func.count()).select_from(Client)) == 0  # nothing in scribble_clients
    finally:
        cfg.client_model = None
        cfg.extras.pop("can_operate_on", None)
        cfg.extras.pop("engagement_summaries", None)


def test_import_links_uuid_client_id_when_mounted_host_uses_uuid_ids(client, session_factory, app):
    """Lotek v2 host client ids are UUIDs, not ints (see plans/v2-rearchitecture-decision.md). import_board
    stores ``summ["client_id"]`` through SoftHostId; this proves a UUID client link round-trips (not parsed
    to ``None``) AND that the link is real -- ``resolve_client`` resolves the HostClientUuid row, not just
    an id sitting unresolved in the column."""
    cfg = app.extensions["scribble"]
    cfg.client_model = HostClientUuid
    core = uuid.uuid7()
    try:
        with session_factory() as db:
            existing = HostClientUuid(name="Hosted Co (v2)")
            db.add(existing)
            db.commit()
            existing_id = existing.id
        assert isinstance(existing_id, uuid.UUID)

        resp = _import(client, cfg, core_id=core, name="Hosted Co v2 Pentest", client_id=existing_id)
        assert resp.status_code == 302

        with app.app_context(), session_factory() as db:
            eng = db.query(ReportBoard).filter_by(name="Hosted Co v2 Pentest").one()
            assert eng.client_id == existing_id, "the UUID client link must NOT be dropped"
            assert isinstance(eng.client_id, uuid.UUID)
            resolved = eng.resolve_client(db)
            assert isinstance(resolved, HostClientUuid)
            assert resolved.name == "Hosted Co (v2)"
    finally:
        cfg.client_model = None
        cfg.extras.pop("can_operate_on", None)
        cfg.extras.pop("engagement_summaries", None)


def test_import_persists_uuid_owner_id_when_mounted_host_uses_uuid_ids(client, session_factory, app):
    """The other half of the same bug: ``scribble.deps.current_actor_id()`` feeds ``ReportBoard.owner_id``
    attribution on import. Its old ``isinstance(ident, int)`` check silently turned a v2 host's UUID actor
    id into ``None`` -- ``owner_id`` NULL on every mounted create, no error, attribution just gone. Proves
    it now persists as the real UUID, not None."""
    cfg = app.extensions["scribble"]
    actor_id = uuid.uuid4()
    core = uuid.uuid7()
    cfg.extras["current_actor"] = lambda: SimpleNamespace(id=actor_id, username="v2.operator")
    with session_factory() as db:
        c = Client(name="Attributed Co")
        db.add(c)
        db.commit()
        cid = c.id
    try:
        resp = _import(client, cfg, core_id=core, name="Attributed v2 Pentest", client_id=cid)
        assert resp.status_code == 302

        with session_factory() as db:
            eng = db.query(ReportBoard).filter_by(name="Attributed v2 Pentest").one()
            assert eng.owner_id is not None, "owner_id must not be silently dropped for a UUID actor id"
            assert eng.owner_id == actor_id
            assert isinstance(eng.owner_id, uuid.UUID)
    finally:
        cfg.extras.pop("current_actor", None)
        cfg.extras.pop("can_operate_on", None)
        cfg.extras.pop("engagement_summaries", None)


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
        finding = BoardFinding.from_lotek_finding(fake_finding)
    assert finding.severity == Severity.high
    assert isinstance(finding.severity, Severity)


def test_from_lotek_finding_uses_injected_host_severity_when_mounted(app):
    HostSeverity = _host_severity_enum()
    cfg = app.extensions["scribble"]
    cfg.severity_enum = HostSeverity
    try:
        fake_finding = SimpleNamespace(title="SQLi", severity=SimpleNamespace(value="high"))
        with app.app_context():
            finding = BoardFinding.from_lotek_finding(fake_finding)
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
    finding = BoardFinding.from_lotek_finding(fake_finding)
    assert finding.severity == Severity.critical
    assert isinstance(finding.severity, Severity)


def test_from_lotek_finding_sets_source_finding_id_from_the_lotek_finding():
    """Promoting a Lotek scan finding stamps ``source_finding_id`` from the source's own id, so the
    promote flow can later dedup (has this Lotek finding already been promoted here?)."""
    fake_finding = SimpleNamespace(id=42, title="SQLi", severity=SimpleNamespace(value="high"))
    finding = BoardFinding.from_lotek_finding(fake_finding)
    assert finding.source_finding_id == 42


def test_from_lotek_finding_source_finding_id_override_wins():
    """An explicit ``source_finding_id=`` override (e.g. re-pointing at a different id) beats the
    finding's own id, matching the general override-merge pattern of ``from_lotek_finding``."""
    fake_finding = SimpleNamespace(id=42, title="SQLi", severity=SimpleNamespace(value="high"))
    finding = BoardFinding.from_lotek_finding(fake_finding, source_finding_id=99)
    assert finding.source_finding_id == 99
